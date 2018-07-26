# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

import logging
import math
import os
import pickle
import threading
import time
import uuid
import errno

from parlai.mturk.core.agents import AssignState
from parlai.mturk.core.socket_manager import Packet, SocketManager
from parlai.mturk.core.worker_manager import WorkerManager
import parlai.mturk.core.data_model as data_model
import parlai.mturk.core.mturk_utils as mturk_utils
import parlai.mturk.core.server_utils as server_utils
import parlai.mturk.core.shared_utils as shared_utils

# Timeout before cancelling a world start
WORLD_START_TIMEOUT = 11

# Multiplier to apply when creating hits to ensure worker availibility
HIT_MULT = 1.5

# 6 minute timeout to ensure only one thread updates the time logs.
# Those update once daily in a 3 minute window
RESET_TIME_LOG_TIMEOUT = 360

TIME_LOGS_FILE_NAME = 'working_time.pickle'
TIME_LOGS_FILE_LOCK = 'working_time.lock'

AMAZON_SNS_NAME = 'AmazonMTurk'
SNS_ASSIGN_ABANDONDED = 'AssignmentAbandoned'
SNS_ASSIGN_SUBMITTED = 'AssignmentSubmitted'
SNS_ASSIGN_RETURNED = 'AssignmentReturned'


parent_dir = os.path.dirname(os.path.abspath(__file__))


class LockFile():
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY

    def __init__(self, filename):
        self.filename = filename
        self.fd = None

    def __enter__(self):
        while self.fd is None:
            try:
                self.fd = os.open(self.filename, self.flags)
            except OSError as e:
                if e.errno == errno.EEXIST:  # Failed as the file exists.
                    pass
            time.sleep(shared_utils.THREAD_SHORT_SLEEP)
        return self

    def __exit__(self, *args):
        os.close(self.fd)
        os.remove(self.filename)


class MTurkManager():
    """Manages interactions between MTurk agents as well as direct interactions
    between a world and the MTurk server.
    """

    def __init__(self, opt, mturk_agent_ids, is_test=False):
        """Create an MTurkManager using the given setup opts and a list of
        agent_ids that will participate in each conversation
        """
        try:
            import parlai_internal.mturk.configs as local_configs
            opt = local_configs.apply_default_opts(opt)
        except Exception:
            # not all users will be drawing configs from internal settings
            pass

        self.opt = opt
        if self.opt['unique_worker'] or \
                self.opt['unique_qual_name'] is not None:
            self.opt['allowed_conversations'] = 1
        self.server_url = None
        self.topic_arn = None
        self.port = 443
        self.task_group_id = None
        self.run_id = None
        self.mturk_agent_ids = mturk_agent_ids
        self.task_files_to_copy = None
        self.is_sandbox = opt['is_sandbox']
        self.worker_pool_change_condition = threading.Condition()
        self.onboard_function = None
        self.num_conversations = opt['num_conversations']
        self.required_hits = math.ceil(
            self.num_conversations * len(self.mturk_agent_ids) * HIT_MULT
        )
        self.minimum_messages = opt.get('min_messages', 0)
        self.auto_approve_delay = opt.get('auto_approve_delay', 4*7*24*3600)
        self.has_time_limit = opt.get('max_time', 0) > 0
        self.socket_manager = None
        self.worker_manager = WorkerManager(self, opt)
        self.is_test = is_test
        self.is_unique = False
        self._init_logs()

    # Helpers and internal manager methods #

    def _init_state(self):
        """Initialize everything in the worker, task, and thread states"""
        # TODO handle pooling in own class, note this is an agent_pool
        self.worker_pool = []

        # TODO move some state to DB
        self.hit_id_list = []
        self.assignment_to_onboard_thread = {}
        self.conversation_index = 0
        self.started_conversations = 0
        self.completed_conversations = 0
        self.task_threads = []
        self.accepting_workers = True
        self._reset_time_logs(init_load=True)
        self.qualifications = None
        self.time_limit_checked = time.time()

    def _init_logs(self):
        """Initialize logging settings from the opt"""
        shared_utils.set_is_debug(self.opt['is_debug'])
        shared_utils.set_log_level(self.opt['log_level'])

    def _reset_time_logs(self, init_load=False, force=False):
        # Uses a weak lock file to try to prevent clobbering between threads
        file_path = os.path.join(parent_dir, TIME_LOGS_FILE_NAME)
        file_lock = os.path.join(parent_dir, TIME_LOGS_FILE_LOCK)
        with LockFile(file_lock) as _lock_file:
            assert _lock_file is not None
            if os.path.exists(file_path):
                with open(file_path, 'rb+') as time_log_file:
                    existing_times = pickle.load(time_log_file)
                    # Initial loads should only reset if it's been a day,
                    # otherwise only need to check an hour for safety
                    compare_time = 24 * 60 * 60 if init_load else 60 * 60
                    if time.time() - existing_times['last_reset'] < \
                            compare_time and not force:
                        return  # do nothing if it's been less than a day
                    reset_workers = list(existing_times.keys())
                    reset_workers.remove('last_reset')
                    self.worker_manager.un_time_block_workers(reset_workers)

                # Reset the time logs
                os.remove(file_path)
            # new time logs
            with open(file_path, 'wb+') as time_log_file:
                time_logs = {'last_reset': time.time()}
                pickle.dump(time_logs, time_log_file,
                            pickle.HIGHEST_PROTOCOL)

    def _log_working_time(self, mturk_worker):
        additional_time = time.time() - mturk_worker.creation_time
        worker_id = mturk_worker.worker_id
        file_path = os.path.join(parent_dir, TIME_LOGS_FILE_NAME)
        file_lock = os.path.join(parent_dir, TIME_LOGS_FILE_LOCK)
        with LockFile(file_lock) as _lock_file:
            assert _lock_file is not None
            if not os.path.exists(file_path):
                self._reset_time_logs()
            with open(file_path, 'rb+') as time_log_file:
                existing_times = pickle.load(time_log_file)
                total_work_time = existing_times.get(worker_id, 0)
                total_work_time += additional_time
                existing_times[worker_id] = total_work_time
            os.remove(file_path)
            with open(file_path, 'wb+') as time_log_file:
                pickle.dump(existing_times, time_log_file,
                            pickle.HIGHEST_PROTOCOL)

        if total_work_time > int(self.opt.get('max_time')):
            self.worker_manager.time_block_worker(worker_id)

    def _move_workers_to_waiting(self, workers):
        """Put all workers into waiting worlds, expire them if no longer
        accepting workers. If the worker is already final, clean it
        """
        for worker in workers:
            worker_id = worker.worker_id
            assignment_id = worker.assignment_id
            if worker.is_final():
                worker.reduce_state()
                self.socket_manager.close_channel(worker.get_connection_id())
                continue

            conversation_id = 'w_{}'.format(uuid.uuid4())
            if self.accepting_workers:
                # Move the worker into a waiting world
                self.worker_manager.change_agent_conversation(
                    agent=worker,
                    conversation_id=conversation_id,
                    new_agent_id='waiting',
                )
            else:
                self.force_expire_hit(worker_id, assignment_id)

    def _expire_onboarding_pool(self):
        """Expire any worker that is in an onboarding thread"""
        def expire_func(agent):
            self.force_expire_hit(agent.worker_id, agent.assignment_id)

        def is_onboard(agent):
            return agent.get_status() == AssignState.STATUS_ONBOARDING

        self.worker_manager.map_over_agents(expire_func, is_onboard)

    def _expire_worker_pool(self):
        """Expire all workers in the worker pool"""
        for agent in self.worker_pool:
            self.force_expire_hit(agent.worker_id, agent.assignment_id)

    def _get_unique_pool(self, eligibility_function):
        """Return a filtered version of the worker pool where each worker is
        only listed a maximum of one time. In sandbox this is overridden for
        testing purposes, and the same worker can be returned more than once
        """
        pool = [w for w in self.worker_pool if not w.hit_is_returned]
        if eligibility_function['multiple'] is True:
            workers = eligibility_function['func'](pool)
        else:
            workers = [w for w in pool if eligibility_function['func'](w)]

        unique_workers = []
        unique_worker_ids = []
        for w in workers:
            if (self.is_sandbox) or (w.worker_id not in unique_worker_ids):
                unique_workers.append(w)
                unique_worker_ids.append(w.worker_id)
        return unique_workers

    def _add_agent_to_pool(self, agent):
        """Add a single agent to the pool"""
        if agent not in self.worker_pool:
            # Add the worker to pool
            with self.worker_pool_change_condition:
                shared_utils.print_and_log(
                    logging.DEBUG,
                    "Adding worker {} to pool.".format(agent.worker_id)
                )
                self.worker_pool.append(agent)

    def _handle_worker_disconnect(self, worker_id, assignment_id):
        """Mark a worker as disconnected and send a message to all agents in
        his conversation that a partner has disconnected.
        """
        self.worker_manager.handle_worker_disconnect(
            worker_id, assignment_id, self._handle_partner_disconnect)

    def _handle_partner_disconnect(self, agent):
        """Send a message to an agent notifying them that a partner has
        disconnected and we marked the HIT as complete for them
        """
        if agent is not None and not agent.is_final():
            # Update the assignment state
            agent.some_agent_disconnected = True
            agent_messages = [m for m in agent.get_messages()
                              if 'id' in m and m['id'] == agent.id]
            if len(agent_messages) < self.minimum_messages:
                agent.set_status(AssignState.STATUS_PARTNER_DISCONNECT_EARLY)
            else:
                agent.set_status(AssignState.STATUS_PARTNER_DISCONNECT)

            # Create and send the command
            data = agent.get_inactive_command_data()
            self.send_command(agent.worker_id, agent.assignment_id, data)

    def _restore_worker_state(self, worker_id, assignment_id):
        """Send a command to restore the state of an agent who reconnected"""
        agent = self.worker_manager._get_agent(worker_id, assignment_id)

        if agent is not None:
            agent.alived = False
            # Return an agent to their conversation, then restore the state
            self.worker_manager.change_agent_conversation(
                agent=agent,
                conversation_id=agent.conversation_id,
                new_agent_id=agent.id,
            )

            def send_state_data():
                while not agent.alived and not agent.hit_is_expired:
                    time.sleep(shared_utils.THREAD_SHORT_SLEEP)

                data = {
                    'text': data_model.COMMAND_RESTORE_STATE,
                    'messages': agent.get_messages(),
                    'last_command': agent.get_last_command(),
                }
                self.send_command(worker_id, assignment_id, data)

            state_thread = threading.Thread(target=send_state_data)
            state_thread.daemon = True
            state_thread.start()


    def _setup_socket(self, timeout_seconds=None):
        """Set up a socket_manager with defined callbacks"""
        socket_server_url = self.server_url
        if (self.opt['local']):  # skip some hops for local stuff
            socket_server_url = "https://localhost"
        self.socket_manager = SocketManager(
            socket_server_url,
            self.port,
            self._on_alive,
            self._on_new_message,
            self._on_socket_dead,
            self.task_group_id,
            socket_dead_timeout=timeout_seconds,
        )

    def _on_alive(self, pkt):
        """Update MTurkManager's state when a worker sends an
        alive packet. This asks the socket manager to open a new channel and
        then handles ensuring the worker state is consistent
        """
        shared_utils.print_and_log(
            logging.DEBUG,
            'on_agent_alive: {}'.format(pkt)
        )
        worker_id = pkt.data['worker_id']
        hit_id = pkt.data['hit_id']
        assign_id = pkt.data['assignment_id']
        conversation_id = pkt.data['conversation_id']

        if not assign_id:
            # invalid assignment_id is an auto-fail
            shared_utils.print_and_log(
                logging.WARN,
                'Agent ({}) with no assign_id called alive'.format(worker_id)
            )
            return

        # Open a channel if it doesn't already exist
        self.socket_manager.open_channel(worker_id, assign_id)

        # Get a state for this worker, create if non existing
        worker_state = self.worker_manager.worker_alive(worker_id)

        if not worker_state.has_assignment(assign_id):
            # New connection for the worker. First ensure that this connection
            # isn't violating our uniqueness constraints
            if self.is_unique and worker_state.completed_assignments() > 0:
                text = (
                    'You have already participated in this HIT the maximum '
                    'number of times. This HIT is now expired. '
                    'Please return the HIT.'
                )
                self.force_expire_hit(worker_id, assign_id, text)
                return

            # Ensure we are still accepting workers
            if not self.accepting_workers:
                self.force_expire_hit(worker_id, assign_id)

            # Ensure worker has not exceeded concurrent convo cap
            convs = worker_state.active_conversation_count()
            allowed_convs = self.opt['allowed_conversations']
            if allowed_convs > 0 and convs >= allowed_convs:
                text = ('You can participate in only {} of these HITs at '
                        'once. Please return this HIT and finish your '
                        'existing HITs before accepting more.'.format(
                            allowed_convs
                        ))
                self.force_expire_hit(worker_id, assign_id, text)

            # Initialize a new agent for this worker
            self.worker_manager.assign_task_to_worker(
                hit_id, assign_id, worker_id
            )
            agent = self.worker_manager._get_agent(worker_id, assign_id)
            self._onboard_new_worker(agent)

        else:
            # Reconnecting worker
            agent = self.worker_manager._get_agent(worker_id, assign_id)
            agent.log_reconnect()
            agent.alived = True
            if agent.get_status() == AssignState.STATUS_NONE:
                # See if assigned an onboarding world, update state if so
                if self.is_onboarding_world(conversation_id):
                    agent.set_status(AssignState.STATUS_ONBOARDING)
                    return
                if self.is_waiting_world(conversation_id):
                    agent.set_status(AssignState.STATUS_WAITING)
                    self._add_agent_to_pool(agent)
                    return
                # Reconnecting before even being given a world. Kill the hit
                # so that on a reconnect they can get a new one assigned and
                # the resources of the first one are cleaned.
                self.force_expire_hit(worker_id, assign_id)
                return
            elif agent.get_status() == AssignState.STATUS_ONBOARDING:
                # See if moved to a waiting world, update state if so
                if self.is_waiting_world(conversation_id):
                    agent.set_status(AssignState.STATUS_WAITING)
                    self._add_agent_to_pool(agent)
                    return
                # Reconnecting to the onboarding world should either restore
                # state or expire (if workers are no longer being accepted
                # for this task)
                if not self.accepting_workers:
                    self.force_expire_hit(worker_id, assign_id)
                elif not conversation_id:
                    self._restore_worker_state(worker_id, assign_id)
            elif agent.get_status() == AssignState.STATUS_WAITING:
                if self.is_task_world(conversation_id):
                    agent.set_status(AssignState.STATUS_IN_TASK)
                    agent.clear_messages()
                    return
                # Reconnecting in waiting is either the first reconnect after
                # being told to wait or a waiting reconnect. Restore state if
                # no information is held, and add to the pool if not already in
                # the pool
                if not conversation_id:
                    self._restore_worker_state(worker_id, assign_id)
                self._add_agent_to_pool(agent)
            elif agent.get_status() == AssignState.STATUS_IN_TASK:
                # Reconnecting to the onboarding world or to a task world
                # should resend the messages already in the conversation
                if not conversation_id:
                    self._restore_worker_state(worker_id, assign_id)
                agent.clear_messages()
            elif (agent.get_status() == AssignState.STATUS_DISCONNECT or
                  agent.get_status() == AssignState.STATUS_DONE or
                  agent.get_status() == AssignState.STATUS_EXPIRED or
                  agent.get_status() == AssignState.STATUS_RETURNED or
                  agent.get_status() == AssignState.STATUS_PARTNER_DISCONNECT):
                # inform the connecting user in all of these cases that the
                # task is no longer workable, use appropriate message
                data = agent.get_inactive_command_data()
                self.send_command(worker_id, assign_id, data)

    def _handle_mturk_message(self, pkt):
        assignment_id = pkt.assignment_id
        agent = self.worker_manager.get_agent_for_assignment(assignment_id)
        if agent is None:
            return

        mturk_event_type = pkt.data['text']
        if mturk_event_type == SNS_ASSIGN_RETURNED:
            agent.hit_is_returned = True
            # Treat as a socket_dead event
            self._on_socket_dead(agent.worker_id, assignment_id)
        elif mturk_event_type == SNS_ASSIGN_ABANDONDED:
            agent.set_hit_is_abandoned()
            # Treat as a socket_dead event
            self._on_socket_dead(agent.worker_id, assignment_id)
        elif mturk_event_type == SNS_ASSIGN_SUBMITTED:
            # Socket dead already called, just mark as complete
            agent.hit_is_complete = True

    def _on_new_message(self, pkt):
        """Put an incoming message onto the correct agent's message queue and
        add it to the proper message thread as long as the agent is active
        """
        if pkt.sender_id == AMAZON_SNS_NAME:
            self._handle_mturk_message(pkt)
            return
        self.worker_manager.route_packet(pkt)

    def _on_socket_dead(self, worker_id, assignment_id):
        """Handle a disconnect event, update state as required and notifying
        other agents if the disconnected agent was in conversation with them

        returns False if the socket death should be ignored and the socket
        should stay open and not be considered disconnected
        """
        agent = self.worker_manager._get_agent(worker_id, assignment_id)
        if agent is None:
            # This worker never registered, so we don't do anything
            return

        shared_utils.print_and_log(
            logging.DEBUG,
            'Worker {} disconnected from {} in status {}'.format(
                worker_id,
                agent.conversation_id,
                agent.get_status()
            )
        )

        if agent.get_status() == AssignState.STATUS_NONE:
            # Agent never made it to onboarding, delete
            agent.set_status(AssignState.STATUS_DISCONNECT)
            agent.reduce_state()
        elif agent.get_status() == AssignState.STATUS_ONBOARDING:
            # Agent never made it to task pool, the onboarding thread will die
            # and delete the agent if we mark it as a disconnect
            agent.set_status(AssignState.STATUS_DISCONNECT)
            agent.disconnected = True
        elif agent.get_status() == AssignState.STATUS_WAITING:
            # agent is in pool, remove from pool and delete
            if agent in self.worker_pool:
                with self.worker_pool_change_condition:
                    self.worker_pool.remove(agent)
            agent.set_status(AssignState.STATUS_DISCONNECT)
            agent.reduce_state()
        elif agent.get_status() == AssignState.STATUS_IN_TASK:
            self._handle_worker_disconnect(worker_id, assignment_id)
            agent.disconnected = True
        elif agent.get_status() == AssignState.STATUS_DONE:
            # It's okay if a complete assignment socket dies, but wait for the
            # world to clean up the resource
            return

        self.socket_manager.close_channel(agent.get_connection_id())

    def _onboard_new_worker(self, mturk_agent):
        """Handle creating an onboarding thread and moving an agent through
        the onboarding process, updating the state properly along the way
        """
        # get state variable in question
        worker_id = mturk_agent.worker_id
        assignment_id = mturk_agent.assignment_id

        def _onboard_function(mturk_agent):
            """Onboarding wrapper to set state to onboarding properly"""
            if self.onboard_function:
                conversation_id = 'o_'+str(uuid.uuid4())
                self.worker_state.change_agent_conversation(
                    agent=mturk_agent,
                    conversation_id=conversation_id,
                    new_agent_id='onboarding',
                )
                # Wait for turker to be in onboarding status
                mturk_agent.wait_for_status(AssignState.STATUS_ONBOARDING)
                # call onboarding function
                self.onboard_function(mturk_agent)

            # once onboarding is done, move into a waiting world
            self._move_workers_to_waiting([mturk_agent])

        if assignment_id not in self.assignment_to_onboard_thread:
            # Start the onboarding thread and run it
            onboard_thread = threading.Thread(
                target=_onboard_function,
                args=(mturk_agent,),
                name='onboard-{}-{}'.format(worker_id, assignment_id)
            )
            onboard_thread.daemon = True
            onboard_thread.start()

            self.assignment_to_onboard_thread[assignment_id] = onboard_thread

    def _no_workers_incomplete(self, agents):
        """Return True if all the given agents completed their task"""
        for agent in agents:
            if agent.is_final() and agent.get_status() != \
                    AssignState.STATUS_DONE:
                return False
        return True

    def _check_time_limit(self):
        if time.time() - self.time_limit_checked < RESET_TIME_LOG_TIMEOUT:
            return
        if int(time.time()) % (60*60*24) > 600:
            # sync the time resets to ONCE DAILY in a 10 minute window
            return
        self.time_limit_checked = time.time()
        self._reset_time_logs()
        self.worker_manager.un_time_block_workers()

    def is_onboarding_world(self, conversation_id):
        return conversation_id is not None and conversation_id.startswith('o_')

    def is_waiting_world(self, conversation_id):
        return conversation_id is not None and conversation_id.startswith('w_')

    def is_task_world(self, conversation_id):
        return conversation_id is not None and conversation_id.startswith('t_')

    # Manager Lifecycle Functions #

    def setup_server(self, task_directory_path=None):
        """Prepare the MTurk server for the new HIT we would like to submit"""
        fin_word = 'start'
        if self.opt['count_complete']:
            fin_word = 'finish'
        shared_utils.print_and_log(
            logging.INFO,
            '\nYou are going to allow workers from Amazon Mechanical Turk to '
            'be an agent in ParlAI.\nDuring this process, Internet connection '
            'is required, and you should turn off your computer\'s auto-sleep '
            'feature.',
            should_print=True,
        )
        if self.opt['max_connections'] == 0:
            shared_utils.print_and_log(
                logging.INFO,
                'Enough HITs will be created to fulfill {} times the '
                'number of conversations requested, extra HITs will be expired'
                ' once the desired conversations {}.'
                ''.format(HIT_MULT, fin_word),
                should_print=True,
            )
        else:
            shared_utils.print_and_log(
                logging.INFO,
                'Enough HITs will be launched over time '
                'up to a max of {} times the amount requested until the '
                'desired number of conversations {}.'
                ''.format(HIT_MULT, fin_word),
                should_print=True,
            )
        input('Please press Enter to continue... ')
        shared_utils.print_and_log(logging.NOTSET, '', True)

        if self.opt['local'] is True:
            shared_utils.print_and_log(
                logging.INFO,
                "In order to run the server locally, you will need "
                "to have a public HTTPS endpoint (SSL signed) running on "
                "the server you are currently excecuting ParlAI on. Enter "
                "that public URL hostname when prompted and ensure that the "
                "port being used by ParlAI (usually 3000) has external "
                "traffic routed to it.",
                should_print=True,
            )
            input('Please press Enter to continue... ')

        mturk_utils.setup_aws_credentials()

        # See if there's enough money in the account to fund the HITs requested
        num_assignments = self.required_hits
        payment_opt = {
            'type': 'reward',
            'num_total_assignments': num_assignments,
            'reward': self.opt['reward'],  # in dollars
        }
        total_cost = mturk_utils.calculate_mturk_cost(payment_opt=payment_opt)
        if not mturk_utils.check_mturk_balance(
                balance_needed=total_cost,
                is_sandbox=self.opt['is_sandbox']):
            raise SystemExit('Insufficient funds')

        if ((not self.opt['is_sandbox']) and
                (total_cost > 100 or self.opt['reward'] > 1)):
            confirm_string = '$%.2f' % total_cost
            expected_cost = total_cost / HIT_MULT
            expected_string = '$%.2f' % expected_cost
            shared_utils.print_and_log(
                logging.INFO,
                'You are going to create {} HITs at {} per assignment, for a '
                'total cost up to {} after MTurk fees. Please enter "{}" to '
                'confirm and continue, and anything else to cancel.\nNote that'
                ' of the {}, the target amount to spend is {}.'.format(
                    self.required_hits,
                    '$%.2f' % self.opt['reward'],
                    confirm_string,
                    confirm_string,
                    confirm_string,
                    expected_string
                ),
                should_print=True
            )
            check = input('Enter here: ')
            if (check != confirm_string and ('$' + check) != confirm_string):
                raise SystemExit('Cancelling')

        shared_utils.print_and_log(logging.INFO, 'Setting up MTurk server...',
                                   should_print=True)
        self.is_unique = self.opt['unique_worker'] or \
            (self.opt['unique_qual_name'] is not None)
        mturk_utils.create_hit_config(
            task_description=self.opt['task_description'],
            unique_worker=self.is_unique,
            is_sandbox=self.opt['is_sandbox']
        )
        # Poplulate files to copy over to the server
        if not self.task_files_to_copy:
            self.task_files_to_copy = []
        if not task_directory_path:
            task_directory_path = os.path.join(
                self.opt['parlai_home'],
                'parlai',
                'mturk',
                'tasks',
                self.opt['task']
            )
        self.task_files_to_copy.append(
            os.path.join(task_directory_path, 'html', 'cover_page.html'))
        try:
            for file_name in os.listdir(os.path.join(
                    task_directory_path, 'html')):
                self.task_files_to_copy.append(os.path.join(
                    task_directory_path, 'html', file_name
                ))
        except FileNotFoundError:  # noqa F821 we don't support python2
            # No html dir exists
            pass
        for mturk_agent_id in self.mturk_agent_ids + ['onboarding']:
            self.task_files_to_copy.append(os.path.join(
                task_directory_path,
                'html',
                '{}_index.html'.format(mturk_agent_id)
            ))

        # Setup the server with a likely-unique app-name
        task_name = '{}-{}'.format(str(uuid.uuid4())[:8], self.opt['task'])
        self.server_task_name = \
            ''.join(e for e in task_name.lower() if e.isalnum() or e == '-')
        self.server_url = server_utils.setup_server(self.server_task_name,
                                                    self.task_files_to_copy,
                                                    self.opt['local'])
        shared_utils.print_and_log(logging.INFO, self.server_url)

        shared_utils.print_and_log(logging.INFO, "MTurk server setup done.\n",
                                   should_print=True)

    def ready_to_accept_workers(self, timeout_seconds=None):
        """Set up socket to start communicating to workers"""
        shared_utils.print_and_log(logging.INFO,
                                   'Local: Setting up WebSocket...',
                                   not self.is_test)
        self._setup_socket(timeout_seconds=timeout_seconds)

    def start_new_run(self):
        """Clear state to prepare for a new run"""
        self.run_id = str(int(time.time()))
        self.task_group_id = '{}_{}'.format(self.opt['task'], self.run_id)
        self._init_state()
        try:
            self.topic_arn = mturk_utils.setup_sns_topic(
                self.opt['task'],
                self.server_url,
                self.task_group_id
            )
        except Exception:
            self.topic_arn = None
            shared_utils.print_and_log(
                logging.WARN,
                'Botocore couldn\'t subscribe to HIT events, '
                'perhaps you tried to register to localhost?',
                should_print=True
            )

    def set_onboard_function(self, onboard_function):
        self.onboard_function = onboard_function

    def start_task(self, eligibility_function, assign_role_function,
                   task_function):
        """Handle running a task by checking to see when enough agents are
        in the pool to start an instance of the task. Continue doing this
        until the desired number of conversations is had.
        """
        if callable(eligibility_function):
            # Convert legacy eligibility_functions to the new format
            eligibility_function = {
                'multiple': False,
                'func': eligibility_function,
            }
        else:
            # Ensure the eligibility function is valid
            if 'func' not in eligibility_function:
                shared_utils.print_and_log(
                    logging.CRITICAL,
                    "eligibility_function has no 'func'. Cancelling."
                )
                raise Exception(
                    'eligibility_function dict must contain a `func` field '
                    'containing the actual function.'
                )
            elif not callable(eligibility_function['func']):
                shared_utils.print_and_log(
                    logging.CRITICAL,
                    "eligibility_function['func'] not a function. Cancelling."
                )
                raise Exception(
                    "eligibility_function['func'] must contain a function. "
                    "If eligibility_function['multiple'] is set, it should "
                    "filter through the list of workers and only return those "
                    "that are currently eligible to participate. If it is not "
                    "set, it should take in a single worker and return whether"
                    " or not they are eligible."
                )
            if 'multiple' not in eligibility_function:
                eligibility_function['multiple'] = False

        def _task_function(opt, workers, conversation_id):
            """Wait for workers to join the world, then run task function"""
            shared_utils.print_and_log(
                logging.INFO,
                'Starting task {}...'.format(conversation_id)
            )
            shared_utils.print_and_log(
                logging.DEBUG,
                'Waiting for all workers to join the conversation...'
            )
            start_time = time.time()
            while True:
                all_joined = True
                for worker in workers:
                    # check the status of an individual worker assignment
                    if worker.get_status() != AssignState.STATUS_IN_TASK:
                        all_joined = False
                if all_joined:
                    break
                if time.time() - start_time > WORLD_START_TIMEOUT:
                    # We waited but not all workers rejoined, throw workers
                    # back into the waiting pool. Stragglers will disconnect
                    # from there
                    shared_utils.print_and_log(
                        logging.INFO,
                        'Timeout waiting for {}, move back to waiting'.format(
                            conversation_id
                        )
                    )
                    self._move_workers_to_waiting(workers)
                    return
                time.sleep(shared_utils.THREAD_SHORT_SLEEP)

            shared_utils.print_and_log(
                logging.INFO,
                'All workers joined the conversation {}!'.format(
                    conversation_id
                )
            )
            self.started_conversations += 1
            task_function(mturk_manager=self, opt=opt, workers=workers)
            # Delete extra state data that is now unneeded
            for worker in workers:
                worker.clear_messages()

            # Count if it's a completed conversation
            if self._no_workers_incomplete(workers):
                self.completed_conversations += 1
            if self.opt['max_connections'] > 0:  # If using a conv cap
                if self.accepting_workers:  # if still looking for new workers
                    for worker in workers:
                        if worker.submitted_hit():
                            self.create_additional_hits(1)

        while True:
            if self.has_time_limit:
                self._check_time_limit()
            # Loop forever starting task worlds until desired convos are had
            with self.worker_pool_change_condition:
                valid_workers = self._get_unique_pool(eligibility_function)
                needed_workers = len(self.mturk_agent_ids)
                if len(valid_workers) >= needed_workers:
                    # enough workers in pool to start new conversation
                    self.conversation_index += 1
                    new_conversation_id = \
                        't_{}'.format(self.conversation_index)

                    # Add the required number of valid workers to the conv
                    workers = [w for w in valid_workers[:needed_workers]]
                    assign_role_function(workers)
                    # Allow task creator to filter out workers and run
                    # versions of the task that require fewer agents
                    workers = [w for w in workers if w.id is not None]
                    for w in workers:
                        self.worker_manager.change_agent_conversation(
                            agent=w,
                            conversation_id=new_conversation_id,
                            new_agent_id=w.id,
                        )
                        # Remove selected workers from the pool
                        self.worker_pool.remove(w)

                    # Start a new thread for this task world
                    task_thread = threading.Thread(
                        target=_task_function,
                        args=(self.opt, workers, new_conversation_id),
                        name='task-{}'.format(new_conversation_id)
                    )
                    task_thread.daemon = True
                    task_thread.start()
                    self.task_threads.append(task_thread)

            # Once we've had enough conversations, finish and break
            compare_count = self.started_conversations
            if (self.opt['count_complete']):
                compare_count = self.completed_conversations
            if compare_count == self.num_conversations:
                self.accepting_workers = False
                self.expire_all_unassigned_hits()
                self._expire_onboarding_pool()
                self._expire_worker_pool()
                # Wait for all conversations to finish, then break from
                # the while loop
                for thread in self.task_threads:
                    thread.join()
                break
            time.sleep(shared_utils.THREAD_MEDIUM_SLEEP)

    def shutdown(self):
        """Handle any mturk client shutdown cleanup."""
        # Ensure all threads are cleaned and state and HITs are handled
        try:
            self.expire_all_unassigned_hits()
            self._expire_onboarding_pool()
            self._expire_worker_pool()
            self.socket_manager.close_all_channels()
            for assignment_id in self.assignment_to_onboard_thread:
                self.assignment_to_onboard_thread[assignment_id].join()
        except BaseException:
            pass
        finally:
            server_utils.delete_server(self.server_task_name,
                                       self.opt['local'])
            if self.topic_arn is not None:
                mturk_utils.delete_sns_topic(self.topic_arn)
            if self.opt['unique_worker']:
                mturk_utils.delete_qualification(self.unique_qual_id,
                                                 self.is_sandbox)
            self.worker_manager.shutdown()

    # MTurk Agent Interaction Functions #

    def force_expire_hit(self, worker_id, assign_id, text=None, ack_func=None):
        """Send a command to expire a hit to the provided agent, update State
        to reflect that the HIT is now expired
        """
        # Expire in the state
        agent = self.worker_manager._get_agent(worker_id, assign_id)
        if agent is not None:
            if not agent.is_final():
                agent.set_status(AssignState.STATUS_EXPIRED)
                agent.hit_is_expired = True

        if ack_func is None:
            def ack_func(*args):
                self.socket_manager.close_channel(
                    '{}_{}'.format(worker_id, assign_id))
        # Send the expiration command
        if text is None:
            text = ('This HIT is expired, please return and take a new '
                    'one if you\'d want to work on this task.')
        data = {'text': data_model.COMMAND_EXPIRE_HIT, 'inactive_text': text}
        self.send_command(worker_id, assign_id, data, ack_func=ack_func)

    def handle_turker_timeout(self, worker_id, assign_id):
        """To be used by the MTurk agent when the worker doesn't send a message
        within the expected window.
        """
        # Expire the hit for the disconnected user
        text = ('You haven\'t entered a message in too long. As these HITs '
                ' often require real-time interaction, this hit has '
                'been expired and you have been considered disconnected. '
                'Disconnect too frequently and you will be blocked from '
                'working on these HITs in the future.')
        self.force_expire_hit(worker_id, assign_id, text)

        # Send the disconnect event to all workers in the convo
        self._handle_worker_disconnect(worker_id, assign_id)

    def send_message(self, receiver_id, assignment_id, data,
                     blocking=True, ack_func=None):
        """Send a message through the socket manager,
        update conversation state
        """
        data = data.copy()  # Ensure data packet is sent in current state
        data['type'] = data_model.MESSAGE_TYPE_MESSAGE
        # Force messages to have a unique ID
        if 'message_id' not in data:
            data['message_id'] = str(uuid.uuid4())
        conversation_id = None
        agent = self.worker_manager._get_agent(receiver_id, assignment_id)
        if agent is not None:
            conversation_id = agent.conversation_id
        event_id = shared_utils.generate_event_id(receiver_id)
        packet = Packet(
            event_id,
            Packet.TYPE_MESSAGE,
            self.socket_manager.get_my_sender_id(),
            receiver_id,
            assignment_id,
            data,
            conversation_id=conversation_id,
            blocking=blocking,
            ack_func=ack_func
        )

        shared_utils.print_and_log(
            logging.INFO,
            'Manager sending: {}'.format(packet),
            should_print=self.opt['verbose']
        )
        # Push outgoing message to the message thread to be able to resend
        # on a reconnect event
        if agent is not None:
            agent.append_packet(packet.data)
        self.socket_manager.queue_packet(packet)
        return data['message_id']

    def send_command(self, receiver_id, assignment_id, data, blocking=True,
                     ack_func=None):
        """Sends a command through the socket manager,
        update conversation state
        """
        data['type'] = data_model.MESSAGE_TYPE_COMMAND
        event_id = shared_utils.generate_event_id(receiver_id)
        packet = Packet(
            event_id,
            Packet.TYPE_MESSAGE,
            self.socket_manager.get_my_sender_id(),
            receiver_id,
            assignment_id,
            data,
            blocking=blocking,
            ack_func=ack_func
        )

        agent = self.worker_manager._get_agent(receiver_id, assignment_id)
        if (data['text'] != data_model.COMMAND_CHANGE_CONVERSATION and
                data['text'] != data_model.COMMAND_RESTORE_STATE and
                agent is not None):
            # Append last command, as it might be necessary to restore state
            agent.set_last_command(packet.data)

        self.socket_manager.queue_packet(packet)

    def mark_workers_done(self, workers):
        """Mark a group of workers as done to keep state consistent"""
        for worker in workers:
            if self.is_unique:
                self.give_worker_qualification(
                    worker.worker_id,
                    self.unique_qual_name,
                )
            if not worker.is_final():
                worker.set_status(AssignState.STATUS_DONE)
            if self.has_time_limit:
                self._log_working_time(worker)

    def free_workers(self, workers):
        """End completed worker threads"""
        for worker in workers:
            self.socket_manager.close_channel(worker.get_connection_id())

    # Amazon MTurk Server Functions #

    def get_agent_work_status(self, assignment_id):
        return self.worker_manager.get_agent_work_status(assignment_id)

    def get_qualification_list(self, qualifications=None):
        if self.qualifications is not None:
            return self.qualifications

        if qualifications is None:
            qualifications = []

        if not self.is_sandbox:
            try:
                import parlai_internal.mturk.configs as local_configs
                qualifications = \
                    local_configs.set_default_qualifications(qualifications)
            except Exception:
                # not all users will be drawing configs from internal settings
                pass

        if self.opt['disconnect_qualification'] is not None:
            block_qual_id = mturk_utils.find_or_create_qualification(
                self.opt['disconnect_qualification'],
                'A soft ban from using a ParlAI-created HIT due to frequent '
                'disconnects from conversations, leading to negative '
                'experiences for other Turkers and for the requester.',
                self.is_sandbox,
            )
            assert block_qual_id is not None, (
                'Hits could not be created as disconnect qualification could '
                'not be acquired. Shutting down server.'
            )
            qualifications.append({
                'QualificationTypeId': block_qual_id,
                'Comparator': 'DoesNotExist',
                'RequiredToPreview': True
            })

        # Add the soft block qualification if it has been specified
        if self.opt['block_qualification'] is not None:
            block_qual_id = mturk_utils.find_or_create_qualification(
                self.opt['block_qualification'],
                'A soft ban from this ParlAI-created HIT at the requesters '
                'discretion. Generally used to restrict how frequently a '
                'particular worker can work on a particular task.',
                self.is_sandbox,
            )
            assert block_qual_id is not None, (
                'Hits could not be created as block qualification could not be'
                ' acquired. Shutting down server.'
            )
            qualifications.append({
                'QualificationTypeId': block_qual_id,
                'Comparator': 'DoesNotExist',
                'RequiredToPreview': True
            })

        if self.has_time_limit:
            block_qual_name = '{}-max-daily-time'.format(self.task_group_id)
            if self.opt['max_time_qual'] is not None:
                block_qual_name = self.opt['max_time_qual']
            self.max_time_qual = block_qual_name
            block_qual_id = mturk_utils.find_or_create_qualification(
                block_qual_name,
                'A soft ban from working on this HIT or HITs by this '
                'requester based on a maximum amount of daily work time set '
                'by the requester.',
                self.is_sandbox,
            )
            assert block_qual_id is not None, (
                'Hits could not be created as a time block qualification could'
                ' not be acquired. Shutting down server.'
            )
            qualifications.append({
                'QualificationTypeId': block_qual_id,
                'Comparator': 'DoesNotExist',
                'RequiredToPreview': True
            })

        if self.is_unique:
            self.unique_qual_name = self.opt.get('unique_qual_name')
            if self.unique_qual_name is None:
                self.unique_qual_name = self.task_group_id + '_max_submissions'
            self.unique_qual_id = mturk_utils.find_or_create_qualification(
                self.unique_qual_name,
                'Prevents workers from completing a task too frequently',
                self.is_sandbox,
            )
            qualifications.append({
                'QualificationTypeId': self.unique_qual_id,
                'Comparator': 'DoesNotExist',
                'RequiredToPreview': True
            })

        self.qualifications = qualifications
        return qualifications

    def create_additional_hits(self, num_hits, qualifications=None):
        """Handle creation for a specific number of hits/assignments
        Put created HIT ids into the hit_id_list
        """
        shared_utils.print_and_log(logging.INFO,
                                   'Creating {} hits...'.format(num_hits))

        qualifications = self.get_qualification_list(qualifications)

        hit_type_id = mturk_utils.create_hit_type(
            hit_title=self.opt['hit_title'],
            hit_description='{} (ID: {})'.format(self.opt['hit_description'],
                                                 self.task_group_id),
            hit_keywords=self.opt['hit_keywords'],
            hit_reward=self.opt['reward'],
            # Set to 30 minutes by default
            assignment_duration_in_seconds=self.opt.get(
                'assignment_duration_in_seconds', 30 * 60),
            is_sandbox=self.opt['is_sandbox'],
            qualifications=qualifications,
            auto_approve_delay=self.auto_approve_delay,
        )
        mturk_chat_url = '{}/chat_index?task_group_id={}'.format(
            self.server_url,
            self.task_group_id
        )
        shared_utils.print_and_log(logging.INFO, mturk_chat_url)
        mturk_page_url = None

        if self.topic_arn is not None:
            mturk_utils.subscribe_to_hits(
                hit_type_id,
                self.is_sandbox,
                self.topic_arn
            )

        for _i in range(num_hits):
            mturk_page_url, hit_id = mturk_utils.create_hit_with_hit_type(
                page_url=mturk_chat_url,
                hit_type_id=hit_type_id,
                num_assignments=1,
                is_sandbox=self.is_sandbox
            )
            self.hit_id_list.append(hit_id)
        return mturk_page_url

    def create_hits(self, qualifications=None):
        """Create hits based on the managers current config, return hit url"""
        shared_utils.print_and_log(logging.INFO, 'Creating HITs...', True)

        if self.opt['max_connections'] == 0:
            mturk_page_url = self.create_additional_hits(
                num_hits=self.required_hits,
                qualifications=qualifications,
            )
        else:
            mturk_page_url = self.create_additional_hits(
                num_hits=min(self.required_hits, self.opt['max_connections']),
                qualifications=qualifications,
            )

        shared_utils.print_and_log(logging.INFO,
                                   'Link to HIT: {}\n'.format(mturk_page_url),
                                   should_print=True)
        shared_utils.print_and_log(
            logging.INFO,
            'Waiting for Turkers to respond... (Please don\'t close'
            ' your laptop or put your computer into sleep or standby mode.)\n',
            should_print=True
        )
        return mturk_page_url

    def get_hit(self, hit_id):
        """Get hit from mturk by hit_id"""
        client = mturk_utils.get_mturk_client(self.is_sandbox)
        return client.get_hit(HITId=hit_id)

    def get_assignment(self, assignment_id):
        """Gets assignment from mturk by assignment_id. Only works if the
        assignment is in a completed state
        """
        client = mturk_utils.get_mturk_client(self.is_sandbox)
        return client.get_assignment(AssignmentId=assignment_id)

    def get_assignments_for_hit(self, hit_id):
        """Get completed assignments for a hit"""
        client = mturk_utils.get_mturk_client(self.is_sandbox)
        assignments_info = client.list_assignments_for_hit(HITId=hit_id)
        return assignments_info.get('Assignments', [])

    def expire_all_unassigned_hits(self):
        """Move through the whole hit_id list and attempt to expire the
        HITs, though this only immediately expires those that aren't assigned.
        """
        # TODO note and mark assigned hits as ones to be expired later
        shared_utils.print_and_log(logging.INFO,
                                   'Expiring all unassigned HITs...',
                                   should_print=not self.is_test)
        completed_ids = self.worker_manager.get_complete_hits()
        for hit_id in self.hit_id_list:
            if hit_id not in completed_ids:
                # TODO get confirmation that the HIT is acutally expired
                mturk_utils.expire_hit(self.is_sandbox, hit_id)

    def approve_work(self, assignment_id):
        """approve work for a given assignment through the mturk client"""
        client = mturk_utils.get_mturk_client(self.is_sandbox)
        client.approve_assignment(AssignmentId=assignment_id)
        shared_utils.print_and_log(
            logging.INFO,
            'Assignment {} approved.'
            ''.format(assignment_id),
        )

    def reject_work(self, assignment_id, reason):
        """reject work for a given assignment through the mturk client"""
        client = mturk_utils.get_mturk_client(self.is_sandbox)
        client.reject_assignment(
            AssignmentId=assignment_id,
            RequesterFeedback=reason
        )
        shared_utils.print_and_log(
            logging.INFO,
            'Assignment {} rejected for reason {}.'
            ''.format(assignment_id, reason),
        )

    def approve_assignments_for_hit(self, hit_id, override_rejection=False):
        """Approve work for assignments associated with a given hit, through
        mturk client
        """
        client = mturk_utils.get_mturk_client(self.is_sandbox)
        assignments = self.get_assignments_for_hit(hit_id)
        for assignment in assignments:
            assignment_id = assignment['AssignmentId']
            client.approve_assignment(AssignmentId=assignment_id,
                                      OverrideRejection=override_rejection)

    def block_worker(self, worker_id, reason):
        """Block a worker by id using the mturk client, passes reason along"""
        client = mturk_utils.get_mturk_client(self.is_sandbox)
        client.create_worker_block(WorkerId=worker_id, Reason=reason)
        shared_utils.print_and_log(
            logging.INFO,
            'Worker {} blocked for reason {}.'
            ''.format(worker_id, reason),
        )

    def soft_block_worker(self, worker_id, qual='block_qualification'):
        """Soft block a worker by giving the worker the block qualification"""
        qual_name = self.opt.get(qual, None)
        assert qual_name is not None, ('No qualification {} has been specified'
                                       ''.format(qual))
        self.give_worker_qualification(worker_id, qual_name)

    def un_soft_block_worker(self, worker_id, qual='block_qualification'):
        """Remove a soft block from a worker by removing a block qualification
            from the worker"""
        qual_name = self.opt.get(qual, None)
        assert qual_name is not None, ('No qualification {} has been specified'
                                       ''.format(qual))
        self.remove_worker_qualification(worker_id, qual_name)

    def give_worker_qualification(self, worker_id, qual_name, qual_value=None):
        """Give a worker a particular qualification"""
        qual_id = mturk_utils.find_qualification(qual_name, self.is_sandbox)
        if qual_id is False or qual_id is None:
            shared_utils.print_and_log(
                logging.WARN,
                'Could not give worker {} qualification {}, as the '
                'qualification could not be found to exist.'
                ''.format(worker_id, qual_name),
                should_print=True
            )
            return
        mturk_utils.give_worker_qualification(worker_id, qual_id, qual_value,
                                              self.is_sandbox)
        shared_utils.print_and_log(
            logging.INFO,
            'gave {} qualification {}'.format(worker_id, qual_name),
            should_print=True
        )

    def remove_worker_qualification(self, worker_id, qual_name, reason=''):
        """Remove a qualification from a worker"""
        qual_id = mturk_utils.find_qualification(qual_name, self.is_sandbox)
        if qual_id is False or qual_id is None:
            shared_utils.print_and_log(
                logging.WARN,
                'Could not remove from worker {} qualification {}, as the '
                'qualification could not be found to exist.'
                ''.format(worker_id, qual_name),
                should_print=True
            )
            return
        try:
            mturk_utils.remove_worker_qualification(worker_id, qual_id,
                                                    self.is_sandbox, reason)
            shared_utils.print_and_log(
                logging.INFO,
                'removed {}\'s qualification {}'.format(worker_id, qual_name),
                should_print=True
            )
        except Exception as e:
            shared_utils.print_and_log(
                logging.WARN if not self.has_time_limit else logging.INFO,
                'removing {}\'s qualification {} failed with error {}. This '
                'can be because the worker didn\'t have that qualification.'
                ''.format(worker_id, qual_name, repr(e)),
                should_print=True
            )

    def create_qualification(self, qualification_name, description,
                             can_exist=True):
        """Create a new qualification. If can_exist is set, simply return
        the ID of the existing qualification rather than throw an error
        """
        if not can_exist:
            qual_id = mturk_utils.find_qualification(qualification_name,
                                                     self.is_sandbox)
            if qual_id is not None:
                shared_utils.print_and_log(
                    logging.WARN,
                    'Could not create qualification {}, as it existed'
                    ''.format(qualification_name),
                    should_print=True
                )
                return None
        return mturk_utils.find_or_create_qualification(
            qualification_name,
            description,
            self.is_sandbox
        )

    def pay_bonus(self, worker_id, bonus_amount, assignment_id, reason,
                  unique_request_token):
        """Handles paying bonus to a turker, fails for insufficient funds.
        Returns True on success and False on failure
        """
        total_cost = mturk_utils.calculate_mturk_cost(
            payment_opt={'type': 'bonus', 'amount': bonus_amount}
        )
        if not mturk_utils.check_mturk_balance(balance_needed=total_cost,
                                               is_sandbox=self.is_sandbox):
            shared_utils.print_and_log(
                logging.WARN,
                'Cannot pay bonus. Reason: Insufficient '
                'funds in your MTurk account.',
                should_print=True
            )
            return False

        client = mturk_utils.get_mturk_client(self.is_sandbox)
        # unique_request_token may be useful for handling future network errors
        client.send_bonus(
            WorkerId=worker_id,
            BonusAmount=str(bonus_amount),
            AssignmentId=assignment_id,
            Reason=reason,
            UniqueRequestToken=unique_request_token
        )
        shared_utils.print_and_log(
            logging.INFO,
            'Paid ${} bonus to WorkerId: {}'.format(
                bonus_amount,
                worker_id
            )
        )
        return True

    def email_worker(self, worker_id, subject, message_text):
        """Send an email to a worker through the mturk client"""
        client = mturk_utils.get_mturk_client(self.is_sandbox)
        response = client.notify_workers(
            Subject=subject,
            MessageText=message_text,
            WorkerIds=[worker_id]
        )
        if len(response['NotifyWorkersFailureStatuses']) > 0:
            failure_message = response['NotifyWorkersFailureStatuses'][0]
            return {'failure': failure_message['NotifyWorkersFailureMessage']}
        else:
            return {'success': True}
