"""Send action goals on explicit request and keep a record of what happened to them.

Concepts:
    A goal record is the gateway's memory of one submitted goal: which
    action, the goal values, its UUID, its state, recent feedback, the result
    and all timestamps. Records live in the gateway process, not in the
    browser, so reloading a page or losing the connection neither loses nor
    resends a goal. Records are kept until the gateway stops.

    A request identifier is chosen by the client for each deliberate
    submission. Submitting the same identifier again returns the existing
    record instead of sending a second goal.

    An action client exists only while one of its goals is active. When the
    last goal of an action finishes, its client is removed again, so an idle
    gateway offers and uses nothing on the ROS graph.

    This is the only module that makes the gateway act on ROS. It can send
    and cancel action goals and nothing else.
"""
import threading
import time
import uuid

from backend import ros_interfaces, ros_topics
from backend.gateway import GatewayError

STATES = {0: 'unknown', 1: 'accepted', 2: 'executing', 3: 'canceling', 4: 'succeeded', 5: 'canceled', 6: 'aborted'}
TERMINAL = ('succeeded', 'canceled', 'aborted', 'rejected', 'failed', 'lost')
CANCEL_ANSWERS = {0: 'accepted', 1: 'rejected', 2: 'unknown goal', 3: 'goal already finished'}
FEEDBACK_KEPT = 100
RECORDS_KEPT = 200


class GoalRefused(GatewayError):
    """A goal could not be submitted; ``status`` is the HTTP status that explains why."""

    def __init__(self, message, status):
        """Remember the explanation and the HTTP status for the web layer."""
        super().__init__(message)
        self.status = status


class GoalManager:
    """Own the action clients, the active goal handles and the goal records."""

    def __init__(self, gateway):
        """Start with no clients and no records, attached to a running gateway.

        A one-second timer on the ROS thread tidies up: it notices servers
        that disappeared while a goal was active and removes clients that no
        longer have active goals.
        """
        self.gateway = gateway
        self.lock = threading.Lock()
        self.records = {}
        self.requests = {}
        self.handles = {}
        self.clients = {}
        self.pending = {}
        self.timer = gateway.call(lambda: gateway.node.create_timer(1.0, self.tidy))

    def summary(self, record):
        """Return a copy of a record that is safe to hand to another thread."""
        with self.lock:
            return dict(record, feedback=list(record['feedback']))

    def list(self):
        """Return all goal records, newest first."""
        with self.lock:
            return [dict(record, feedback=list(record['feedback']))
                    for record in sorted(self.records.values(), key=lambda item: item['created_at'], reverse=True)]

    def get(self, goal_id):
        """Return one goal record or ``None`` when the identifier is unknown."""
        with self.lock:
            record = self.records.get(goal_id)
            return dict(record, feedback=list(record['feedback'])) if record else None

    def client_for(self, action, type_name, action_type):
        """Return the action client for an action, creating it on the ROS thread if needed."""
        from rclpy.action import ActionClient
        key = (action, type_name)
        if key not in self.clients:
            self.clients[key] = ActionClient(self.gateway.node, action_type, action)
        return self.clients[key]

    def submit(self, action, type_name, goal, request_id):
        """Send one goal and return its record; a repeated request identifier sends nothing.

        First look up the request identifier: if this submission was already
        handled, return its record marked as a duplicate. Otherwise resolve
        the action type, check the goal values against the Goal definition
        and build the message; invalid values raise ``InvalidValue`` and
        nothing is sent. Then create the client and wait up to two seconds
        for the action server to be discovered; without a server the goal is
        refused. While this waits, the action is marked as pending so the
        tidy timer does not remove the client that is being used. Finally send the goal with a UUID chosen here, so the record
        exists before the server answers. Acceptance, feedback and the result
        update the record later on the ROS thread.
        """
        with self.lock:
            if request_id in self.requests:
                record = self.records[self.requests[request_id]]
                return dict(record, feedback=list(record['feedback']), duplicate=True)
        action_type = ros_interfaces.action_class(type_name)
        message = ros_interfaces.build(action_type.Goal, goal)
        key = (action, type_name)
        with self.lock:
            self.pending[key] = self.pending.get(key, 0) + 1
        try:
            client = self.gateway.call(lambda: self.client_for(action, type_name, action_type))
            deadline = time.monotonic() + 2.0
            while not self.gateway.call(client.server_is_ready):
                if time.monotonic() > deadline:
                    raise GoalRefused(f'No action server is available for {action} ({type_name})', 409)
                time.sleep(0.1)
            return self.gateway.call(lambda: self.start(client, action, type_name, message, request_id))
        finally:
            with self.lock:
                self.pending[key] -= 1
                if not self.pending[key]:
                    del self.pending[key]

    def start(self, client, action, type_name, message, request_id):
        """Create the record and send the goal; runs on the ROS thread.

        The request identifier is checked once more under the lock, so two
        simultaneous submissions with the same identifier still send one goal.
        """
        from unique_identifier_msgs.msg import UUID
        with self.lock:
            if request_id in self.requests:
                record = self.records[self.requests[request_id]]
                return dict(record, feedback=list(record['feedback']), duplicate=True)
            identifier = uuid.uuid4()
            record = dict(goal_id=str(identifier), request_id=request_id, action=action, type=type_name,
                          goal=ros_interfaces.to_json(message), state='sending', created_at=time.time(),
                          accepted_at=None, finished_at=None, feedback=[], feedback_count=0, result=None,
                          result_truncated=False, error=None, cancel_requested_at=None, cancel_answer=None)
            self.records[record['goal_id']] = record
            self.requests[request_id] = record['goal_id']
            while len(self.records) > RECORDS_KEPT:
                oldest = min((item for item in self.records.values() if item['state'] in TERMINAL),
                             key=lambda item: item['created_at'], default=None)
                if oldest is None:
                    break
                del self.records[oldest['goal_id']]
                self.requests.pop(oldest['request_id'], None)
        future = client.send_goal_async(message, feedback_callback=lambda feedback: self.on_feedback(record, feedback),
                                        goal_uuid=UUID(uuid=list(identifier.bytes)))
        future.add_done_callback(lambda done: self.on_response(record, done))
        return self.summary(record)

    def on_response(self, record, future):
        """Record whether the server accepted the goal and, if so, wait for its result."""
        try:
            handle = future.result()
        except Exception as error:
            self.finish(record, 'failed', error=f'Sending the goal failed: {error}')
            return
        if not handle.accepted:
            self.finish(record, 'rejected', error='The action server rejected the goal')
            return
        with self.lock:
            record.update(state='accepted', accepted_at=time.time())
            self.handles[record['goal_id']] = handle
        handle.get_result_async().add_done_callback(lambda done: self.on_result(record, done))

    def on_feedback(self, record, feedback):
        """Append one feedback message to the record, keeping only the most recent ones."""
        with self.lock:
            record['feedback_count'] += 1
            record['feedback'].append(dict(at=time.time(), number=record['feedback_count'],
                                           message=ros_topics.plain(feedback.feedback)))
            del record['feedback'][:-FEEDBACK_KEPT]
            if record['state'] == 'accepted':
                record['state'] = 'executing'

    def on_result(self, record, future):
        """Store the final status and result of a goal."""
        try:
            response = future.result()
        except Exception as error:
            self.finish(record, 'failed', error=f'Waiting for the result failed: {error}')
            return
        state = {}
        result = ros_topics.plain(response.result, state)
        self.finish(record, STATES.get(response.status, 'unknown'), result=result,
                    result_truncated=bool(state.get('truncated')))

    def finish(self, record, state, **fields):
        """Mark a goal as finished and forget its handle."""
        with self.lock:
            if record['state'] in TERMINAL:
                return
            record.update(fields, state=state, finished_at=time.time())
            self.handles.pop(record['goal_id'], None)

    def cancel(self, goal_id):
        """Ask the action server to cancel an active goal and return the updated record.

        Only goals that were accepted and have not finished can be cancelled.
        The server's answer (accepted, rejected, unknown goal, already
        finished) is stored in the record when it arrives. Whether and when
        the goal actually stops is decided by the action server.
        """
        with self.lock:
            record = self.records.get(goal_id)
            handle = self.handles.get(goal_id)
        if record is None:
            raise GoalRefused('Unknown goal identifier', 404)
        if handle is None:
            raise GoalRefused(f"This goal is {record['state']} and cannot be cancelled", 409)

        def request():
            """Send the cancel request on the ROS thread."""
            with self.lock:
                record.update(cancel_requested_at=time.time(), cancel_answer='pending')
            handle.cancel_goal_async().add_done_callback(lambda done: self.on_cancel(record, done))

        self.gateway.call(request)
        return self.summary(record)

    def on_cancel(self, record, future):
        """Store the action server's answer to a cancel request."""
        try:
            response = future.result()
            answer = 'accepted' if response.goals_canceling else CANCEL_ANSWERS.get(response.return_code, 'rejected')
            if response.return_code == 0 and not response.goals_canceling:
                answer = 'rejected'
        except Exception as error:
            answer = f'failed: {error}'
        with self.lock:
            record['cancel_answer'] = answer

    def tidy(self):
        """Keep records truthful and the ROS graph clean; runs every second on the ROS thread.

        Refresh the state of active goals from their handles (accepted,
        executing, canceling). If a goal is active but its action server is
        no longer present, mark it as lost, because no result can arrive and
        the outcome is unknown. Destroy clients that have no active goals and
        no submission in progress.
        """
        with self.lock:
            active = {goal_id: self.records[goal_id] for goal_id in self.handles}
            sending = [record for record in self.records.values() if record['state'] == 'sending']
        with self.lock:
            busy = set(self.pending)
        for goal_id, record in list(active.items()) + [(item['goal_id'], item) for item in sending]:
            key = (record['action'], record['type'])
            client = self.clients.get(key)
            if client is not None and not client.server_is_ready():
                self.finish(record, 'lost', error='The action server disappeared; the outcome of this goal is unknown')
                continue
            busy.add(key)
            handle = self.handles.get(goal_id)
            if handle is not None:
                with self.lock:
                    if record['state'] not in TERMINAL:
                        record['state'] = STATES.get(handle.status, record['state'])
        for key in [key for key in self.clients if key not in busy]:
            self.clients.pop(key).destroy()

    def active_goal_ids(self):
        """Return the identifiers of goals that were accepted and have not finished."""
        with self.lock:
            return list(self.handles)

    def shutdown(self, cancel_active=True, patience=2.0):
        """Prepare for the gateway stopping.

        By default, ask the action servers to cancel every goal that is still
        active and wait briefly for their answers, so that a robot does not
        keep executing goals nobody can watch or cancel any more. With
        ``cancel_active`` false, active goals are left alone and continue on
        their servers. Either way the records end with the gateway process.
        """
        if cancel_active:
            for goal_id in self.active_goal_ids():
                try:
                    self.cancel(goal_id)
                except GatewayError:
                    pass
            deadline = time.monotonic() + patience
            while time.monotonic() < deadline:
                with self.lock:
                    waiting = [record for record in self.records.values() if record['cancel_answer'] == 'pending']
                if not waiting:
                    break
                time.sleep(0.05)
