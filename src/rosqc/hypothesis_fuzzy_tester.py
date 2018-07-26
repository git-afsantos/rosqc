
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

import os
import sys
from threading import Event, Lock
import unittest

import hypothesis
import hypothesis.reporting as reporting
from hypothesis.stateful import (
    RuleBasedStateMachine, rule, precondition, invariant
)
import hypothesis.strategies as strategies
import rospy
import roslaunch
from rosnode import rosnode_ping


###############################################################################
# Utility
###############################################################################

class HiddenPrints(object):
    def __enter__(self):
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._devnull = open(os.devnull, "w")
        sys.stdout = self._devnull
        sys.stderr = self._devnull

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        self._devnull.close()


class OutputCollector(object):
    def __init__(self):
        self.output = []

    def report(self, value):
        self.output.append(value)

    def print_output(self):
        if not self.output:
            return
        print ("==================================="
               "===================================")
        print "Falsifying example"
        print ("-----------------------------------"
               "-----------------------------------")
        for line in self.output:
            print line
        print ("-----------------------------------"
               "-----------------------------------")

###############################################################################
# Settings and Properties
###############################################################################

class TestSettings(object):
    __slots__ = ("launch_files", "nodes_under_test", "expected_rate",
                 "on_setup", "on_teardown", "publishers", "subscribers")

    def __init__(self):
        self.launch_files = []
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
    __slots__ = ("_topic", "_class", "_callback")

    def __init__(self, topic, msg_class, cb=None):
        self._topic = topic
        self._class = msg_class
        self._callback = cb

    def on_message(self, function):
        self._callback = function


class SubscriberProperties(object):
    __slots__ = ("_topic", "_class", "_strategy", "_callback")

    def __init__(self, topic, msg_class, strategy_function, cb=None):
        self._topic = topic
        self._class = msg_class
        self._strategy = strategy_function
        self._callback = cb

    def on_message(self, function):
        self._callback = function


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
    __slots__ = ("topic", "strategy", "callback", "publisher")

    def __init__(self, subscriber_properties):
        self.topic = subscriber_properties._topic
        self.callback = subscriber_properties._callback or self.noop
        self.publisher = rospy.Publisher(
            self.topic, subscriber_properties._class, queue_size=10
        )

    def publish(self, msg):
        self.publisher.publish(msg)
        msg_event = MessageEvent(self.topic, msg, rospy.get_rostime(), True)
        self.callback(msg_event)

    def unregister(self):
        self.publisher.unregister()
        self.callback = None

    def noop(self, event):
        pass


class SubscribeLink(object):
    __slots__ = ("topic", "callback", "subscriber", "event_callback")

    def __init__(self, publisher_properties, event_cb):
        self.topic = publisher_properties._topic
        self.callback = publisher_properties._callback
        self.subscriber = rospy.Subscriber(
                self.topic, publisher_properties._class,
                self.msg_callback if self.callback else self.noop
        )
        self.event_callback = event_cb

    def msg_callback(self, msg):
        msg_event = MessageEvent(self.topic, msg, rospy.get_rostime(), False)
        self.event_callback((self.callback, msg_event))

    def noop(self, msg):
        pass

    def unregister(self):
        self.subscriber.unregister()
        self.callback = None
        self.event_callback = None


class RosInterface(object):
    __slots__ = ("lock", "inbox_flag", "event_queue", "pub", "sub")

    _instance = None

    def __init__(self, cfg):
        self.lock = Lock()
        self.inbox_flag = Event()
        self.event_queue = []
        self.pub = {
            topic: PublishLink(properties)
            for topic, properties in cfg.subscribers.iteritems()
        }
        self.sub = {
            topic: SubscribeLink(properties, self.on_received_event)
            for topic, properties in cfg.publishers.iteritems()
        }

    def on_received_event(self, cb_event_pair):
        with self.lock:
            self.event_queue.append(cb_event_pair)
            self.inbox_flag.set()

    def shutdown(self):
        for sub in self.sub.itervalues():
            sub.unregister()
        for pub in self.pub.itervalues():
            pub.unregister()
        self.sub = {}
        self.pub = {}
        with self.lock:
            self.event_queue = []
            self.inbox_flag.clear()

    def process_inbox(self):
        with self.lock:
            event_queue = self.event_queue
            self.event_queue = []
            self.inbox_flag.clear()
        for cb, event in event_queue:
            cb(event)

    def wait_for_message(self, timeout=None):
        return self.inbox_flag.wait(timeout)

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
# Random Tester
###############################################################################

class RosRandomTester(RuleBasedStateMachine):
    _time_spent_on_testing = 0.0
    _time_spent_sleeping = 0.0
    _time_spent_setting_up = 0.0

    def __init__(self):
        RuleBasedStateMachine.__init__(self)
        t = rospy.get_rostime()
        self.cfg = TestSettings._current
        self.sleep_duration = rospy.Duration(1.0 / self.cfg.expected_rate)
        self.can_spin = False
        self.can_publish = True
        self.did_spin = False
        self.launches = self._start_launch_files(self.cfg.launch_files)
        self.ros = RosInterface(self.cfg)
        self._wait_for_nodes()
        self.ros.wait_for_interfaces()
        if not self.cfg.on_setup is None:
            self.cfg.on_setup()
        self._start_time = rospy.get_rostime()
        t = self._start_time - t
        RosRandomTester._time_spent_setting_up += t.to_sec()
        self._spin_count = 0

    def teardown(self):
        try:
            self._last_spin()
        finally:
            s = self._spin_count * self.sleep_duration.to_sec()
            t0 = rospy.get_rostime()
            t = t0 - self._start_time
            RosRandomTester._time_spent_sleeping += s
            RosRandomTester._time_spent_on_testing += t.to_sec() - s
            if not self.cfg.on_teardown is None:
                self.cfg.on_teardown()
            for launch in self.launches:
                launch.shutdown()
            self.ros.shutdown()
            self._wait_for_nodes(online=False)
            t = rospy.get_rostime() - t0
            RosRandomTester._time_spent_setting_up += t.to_sec()

    def print_end(self):
        if not self.did_spin:
            reporting.report(u"state.spin()")
        RuleBasedStateMachine.print_end(self)

    @precondition(lambda self: not rospy.is_shutdown() and self.can_spin)
    @rule()
    def spin(self):
        self._spin_count += 1
        rospy.sleep(self.sleep_duration)
        self._check_nodes()
        self.ros.process_inbox()
        self.did_spin = True

    def _last_spin(self):
        if not rospy.is_shutdown() and not self.did_spin:
            self._spin_count += 1
            rospy.sleep(self.sleep_duration)
            self.ros.process_inbox()
            for node_name in self.cfg.nodes_under_test:
                assert rosnode_ping(node_name, max_count=1)

    @invariant()
    def check_messages(self):
        self.can_publish = not self.ros.inbox_flag.is_set()
        assert self.ros.check_status() is None

    def _rule_precondition(self):
        return not rospy.is_shutdown() and self.can_publish

    def _start_launch_files(self, launch_paths):
        launches = []
        for launch_path in launch_paths:
            uuid = roslaunch.rlutil.get_or_generate_uuid(None, True)
            roslaunch.configure_logging(uuid)
            launch = roslaunch.parent.ROSLaunchParent(uuid, [launch_path],
                        is_core=False, verbose=False)
            launch.start(auto_terminate=False)
            launches.append(launch)
        return launches

    def _wait_for_nodes(self, timeout=60.0, online=True):
        now = rospy.get_rostime()
        to = rospy.Duration.from_sec(timeout)
        end = now + to
        rate = rospy.Rate(5)
        pending = set(self.cfg.nodes_under_test)
        while now < end and pending:
            for node_name in self.cfg.nodes_under_test:
                if node_name in pending:
                    if rosnode_ping(node_name, max_count=1) is online:
                        pending.remove(node_name)
            rate.sleep()
            now = rospy.get_rostime()
        if pending and online:
            raise LookupError("Failed to find nodes " + str(pending))

    def _check_nodes(self):
        for node_name in self.cfg.nodes_under_test:
            assert rosnode_ping(node_name, max_count=1)

    @staticmethod
    def _gen_publish(topic, name):
        def publish(self, msg):
            self.ros.pub[topic].publish(msg)
            self.can_spin = True
            self.did_spin = False
        publish.__name__ = name
        return publish

    @classmethod
    def gen_rules(cls, cfg):
        for topic, properties in cfg.subscribers.iteritems():
            rule_name = "pub" + topic.replace("/", "__")
            method = RosRandomTester._gen_publish(topic, rule_name)
            pre_wrapper = precondition(cls._rule_precondition)
            rule_wrapper = rule(msg=properties._strategy())
            method = pre_wrapper(rule_wrapper(method))
            method.__name__ = rule_name
            setattr(cls, rule_name, method)

    @classmethod
    def print_times(cls):
        print "Time spent on testing  (s):", cls._time_spent_on_testing
        print "Time spent on sleeping (s):", cls._time_spent_sleeping
        print "Time spent setting up  (s):", cls._time_spent_setting_up

RandomTest = RosRandomTester.TestCase


###############################################################################
# Entry Point
###############################################################################

def run_tests(settings):
    if not isinstance(settings, TestSettings):
        raise ValueError("settings must be of type TestSettings")
    TestSettings._current = settings
    RosRandomTester.gen_rules(settings)
    RosRandomTester.settings = hypothesis.settings(max_examples=1000,
                                                   stateful_step_count=100,
                                                   buffer_size=16384,
                                                   timeout=hypothesis.unlimited)
    collector = OutputCollector()
    reporting.reporter.value = collector.report
    with HiddenPrints():
        unittest.main(module=__name__, argv=[__name__], exit=False)
    collector.print_output()
    RosRandomTester.print_times()
