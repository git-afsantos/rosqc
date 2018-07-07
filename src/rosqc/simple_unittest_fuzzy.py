
#Copyright (c) 2018 Andre Santos
#
#Permission is hereby granted, free of charge, to any person obtaining a copy
#of this software and associated documentation files (the "Software"), to deal
#in the Software without restriction, including without limitation the rights
#to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#copies of the Software, and to permit persons to whom the Software is
#furnished to do so, subject to the following conditions:

#The above copyright notice and this permission notice shall be included in
#all copies or substantial portions of the Software.

#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
#THE SOFTWARE.

###############################################################################
# Imports
###############################################################################

from threading import Lock
import unittest
from traceback import print_exception

import hypothesis
import hypothesis.strategies as strategies
import rospy
import roslaunch
from rosnode import rosnode_ping


###############################################################################
# Settings and Properties
###############################################################################

class TestSettings(object):
    __slots__ = ("launch_file", "nodes_under_test", "expected_rate",
                 "on_setup", "on_teardown", "publishers", "subscribers")

    def __init__(self):
        self.launch_file = None
        self.nodes_under_test = []
        self.expected_rate = 10
        self.publishers = {}    # topic -> PublisherProperties
        self.subscribers = {}   # topic -> SubscriberProperties
        self.on_setup = None
        self.on_teardown = None

    def pub(self, properties):
        if not isinstance(properties, PublisherProperties):
            raise ValueError("properties must be of type PublisherProperties")
        self.publishers[properties._topic] = properties

    def sub(self, properties):
        if not isinstance(properties, SubscriberProperties):
            raise ValueError("properties must be of type SubscriberProperties")
        self.subscribers[properties._topic] = properties


class PublisherProperties(object):
    __slots__ = ("_topic", "_class", "_callbacks")

    def __init__(self, topic, msg_class):
        self._topic = topic
        self._class = msg_class
        self._callbacks = []

    def on_message(self, function):
        self._callbacks.append(function)


class SubscriberProperties(object):
    __slots__ = ("_topic", "_class", "_strategy", "_callbacks")

    def __init__(self, topic, msg_class, strategy_function):
        self._topic = topic
        self._class = msg_class
        self._strategy = strategy_function
        self._callbacks = []

    def on_message(self, function):
        self._callbacks.append(function)


###############################################################################
# ROS Interface Entities
###############################################################################

class MessageEvent(object):
    __slots__ = ("topic", "msg", "time", "published")

    def __init__(self, topic, msg, time, pub):
        self.topic = topic
        self.msg = msg
        self.time = time
        self.published = pub

    def __lt__(self, other):
        return self.time < other.time

    def __le__(self, other):
        return self.time <= other.time

    def __gt__(self, other):
        return self.time > other.time

    def __ge__(self, other):
        return self.time >= other.time

    def __eq__(self, other):
        if not isinstance(self, other.__class__):
            return False
        return self.time == other.time

    def __ne__(self, other):
        return not self.__eq__(other)


class PublishLink(object):
    __slots__ = ("topic", "strategy", "callbacks", "publisher")

    def __init__(self, subscriber_properties):
        self.topic = subscriber_properties._topic
        self.strategy = subscriber_properties._strategy()
        self.callbacks = tuple(subscriber_properties._callbacks)
        self.publisher = rospy.Publisher(
                self.topic, subscriber_properties._class, queue_size=10
        )

    def publish(self, msg):
        self.publisher.publish(msg)
        msg_event = MessageEvent(self.topic, msg, rospy.get_rostime(), True)
        events = []
        for cb in self.callbacks:
            events.append((cb, msg_event))
        return events

    def unregister(self):
        self.publisher.unregister()
        self.callbacks = ()
        self.strategy = None


class SubscribeLink(object):
    __slots__ = ("topic", "callbacks", "subscriber", "event_callback")

    def __init__(self, publisher_properties, event_cb):
        self.topic = publisher_properties._topic
        self.callbacks = tuple(publisher_properties._callbacks)
        self.subscriber = rospy.Subscriber(
                self.topic, publisher_properties._class,
                self.msg_callback if self.callbacks else self.noop
        )
        self.event_callback = event_cb

    def msg_callback(self, msg):
        msg_event = MessageEvent(self.topic, msg, rospy.get_rostime(), False)
        self.event_callback([(cb, msg_event) for cb in self.callbacks])

    def noop(self, msg):
        pass

    def unregister(self):
        self.subscriber.unregister()
        self.callbacks = ()
        self.event_callback = self.noop


class RosInterface(object):
    __slots__ = ("lock", "event_queue", "pub", "sub")

    _instance = None

    def __init__(self, cfg):
        self.lock = Lock()
        self.event_queue = []
        self.pub = {
            topic: PublishLink(properties)
            for topic, properties in cfg.subscribers.iteritems()
        }
        self.sub = {
            topic: SubscribeLink(properties, self.on_received_events)
            for topic, properties in cfg.publishers.iteritems()
        }

    def on_received_events(self, events):
        with self.lock:
            self.event_queue.extend(events)

    def publish(self, pub, msg):
        with self.lock:
            events = pub.publish(msg)
            self.event_queue.extend(events)

    def shutdown(self):
        for sub in self.sub.itervalues():
            sub.unregister()
        for pub in self.pub.itervalues():
            pub.unregister()
        self.sub = {}
        self.pub = {}
        with self.lock:
            self.event_queue = []

    def get_event_queue(self):
        with self.lock:
            event_queue = self.event_queue
            self.event_queue = []
        return event_queue

    def check_status(self):
        for topic, sub in self.sub.iteritems():
            if sub.subscriber.get_num_connections() < 1:
                return topic
        for topic, pub in self.pub.iteritems():
            if pub.publisher.get_num_connections() < 1:
                return topic
        return None

    def wait_for_interfaces(self, timeout=10.0):
        now = rospy.get_rostime()
        to = rospy.Duration.from_sec(timeout)
        end = now + to
        rate = rospy.Rate(5)
        pending = set()
        pending.update(self.sub.keys())
        pending.update(self.pub.keys())
        while now < end and pending:
            for topic, sub in self.sub.iteritems():
                if topic in pending:
                    if sub.subscriber.get_num_connections() > 0:
                        pending.remove(topic)
            for topic, pub in self.pub.iteritems():
                if topic in pending:
                    if pub.publisher.get_num_connections() > 0:
                        pending.remove(topic)
            rate.sleep()
            now = rospy.get_rostime()
        if pending:
            raise LookupError("Failed to connect to topics: " + str(pending))


###############################################################################
# Fuzzy Tester
###############################################################################

class FuzzyTesterResult(unittest.TestResult):
    def addFailure(self, test, err):
        print_exception(err[0], err[1], err[2])
        print "Printing event trace:"
        for event in test.ros_trace:
            print "---"
            if event.published:
                print "Publish on", event.topic
            else:
                print "Receive on", event.topic
            print event.msg
        unittest.TestResult.addFailure(self, test, err)


class RosFuzzyTester(unittest.TestCase):
    def setUp(self):
        self.ros_trace = None
        self.cfg = TestSettings._current
        self.launch = self._start_launch_file(self.cfg.launch_file)
        self.ros = RosInterface(self.cfg)
        self.rate = rospy.Rate(self.cfg.expected_rate)
        self.pub_draw = strategies.sampled_from(self.ros.pub.keys())
        self.iter_draw = strategies.integers(min_value=1,
                            max_value=10 * len(self.ros.pub))
        self._wait_for_nodes()
        self.ros.wait_for_interfaces()
        self.traces = {topic: [] for topic in self.ros.sub}
        if not self.cfg.on_setup is None:
            self.cfg.on_setup()

    def tearDown(self):
        if not self.cfg.on_teardown is None:
            self.cfg.on_teardown()
        self.launch.shutdown()
        self.ros.shutdown()

    def test_fuzzy_interface(self):
        try:
            while not rospy.is_shutdown():
                assert self.ros.check_status() is None
                self.publish_phase()
                self.spin_phase()
        except (rospy.ROSInterruptException, KeyboardInterrupt):
            pass

    def publish_phase(self):
        for i in xrange(self.iter_draw.example()):
            topic = self.pub_draw.example()
            pub = self.ros.pub[topic]
            msg = pub.strategy.example()
            self.ros.publish(pub, msg)

    def spin_phase(self):
        self.rate.sleep()
        for node_name in self.cfg.nodes_under_test:
            assert rosnode_ping(node_name, max_count=1)
        for cb, event in self.ros.get_event_queue():
            try:
                if event.published:
                    for trace in self.traces.itervalues():
                        trace.append(event)
                    cb(event)
                else:
                    self.traces[event.topic].append(event)
                    cb(event)
                    self.traces[event.topic] = [event]
            except BaseException:
                self.ros_trace = self.traces.get(event.topic, [event])
                raise

    def _start_launch_file(self, launch_path):
        uuid = roslaunch.rlutil.get_or_generate_uuid(None, True)
        roslaunch.configure_logging(uuid)
        launch = roslaunch.parent.ROSLaunchParent(uuid, [launch_path],
                    is_core=False, verbose=False)
        launch.start(auto_terminate=False)
        return launch

    def _wait_for_nodes(self, timeout=60.0):
        now = rospy.get_rostime()
        to = rospy.Duration.from_sec(timeout)
        end = now + to
        rate = rospy.Rate(5)
        pending = set(self.cfg.nodes_under_test)
        while now < end and pending:
            for node_name in self.cfg.nodes_under_test:
                if node_name in pending:
                    if rosnode_ping(node_name, max_count=1):
                        pending.remove(node_name)
            rate.sleep()
            now = rospy.get_rostime()
        if pending:
            raise LookupError("Failed to find nodes " + str(pending))


###############################################################################
# Entry Point
###############################################################################

def run_tests(settings):
    if not isinstance(settings, TestSettings):
        raise ValueError("settings must be of type TestSettings")
    TestSettings._current = settings
    unittest.main(module=__name__, argv=[__name__],
            testRunner=unittest.TextTestRunner(resultclass=FuzzyTesterResult))
