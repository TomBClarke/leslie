#!/usr/bin/env python

import rospy
from std_msgs.msg import String
import sys
import json
import state_util
from states import *
from actions import *


if __name__ != '__main__':
    from sys import stderr
    # No one should import this code (stops multiple identical nodes being started)
    print >> stderr, __name__, 'should not be imported!'
    exit(1)


# Mapping of state + action = state
state_machine = {
    # State                Action           -> State
    (AT_HOME,              CALLED_OVER):       MOVE_TO_TABLE,
    (AT_HOME,              FACE_DETECTED):     LISTENING_FOR_TABLE,
    (LISTENING_FOR_TABLE,  CALLED_OVER):       MOVE_TO_TABLE,
    (MOVE_TO_TABLE,        ARRIVED):           AT_TABLE,
    (MOVE_TO_TABLE,        REJECT_TABLE):      LISTENING_FOR_TABLE,
    (AT_TABLE,             READY_TO_LOCK):     LOCKED_AND_WAITING,
    (LOCKED_AND_WAITING,   MOVEMENT_DETECTED): ALARM,
    (ALARM,                FACE_RECOGNISED):   ALARM_REPORT,
    (ALARM_REPORT,         ALARM_HANDLED):     MOVE_TO_HOME,
    (LOCKED_AND_WAITING,   FACE_RECOGNISED):   MOVE_TO_HOME,
    (MOVE_TO_HOME,         ARRIVED):           AT_HOME
}

# Get the starting state from the config.
current_state_id = state_util.get_start_state()


def publish_state(data):
    """
    Publishes the current state to topic '/state'
    :param data: The JSON data to pass in the '/state' topic.
    :type data: dict(str, T)
    """
    global current_state_id
    
    state = {
        'id': current_state_id,
        'data': data
    }

    print 'State:', state
    pub.publish(json.dumps(state))


def action_callback(action_msg):
    """
    Sets a new state given the current state allows for the given action to be taken
    :param action_msg: The message received from the '/action' topic
    :type action_msg: std_msgs.msg.String
    """
    global current_state_id
    
    action_taken = json.loads(action_msg.data)
    action_id = action_taken['id']
    action_data = action_taken['data']
    
    try:
        current_state_id = state_machine[(current_state_id, action_id)]
        publish_state(action_data)
    except KeyError:
        print >> sys.stderr, 'Error updating state! Current state:', current_state_id, 'Action:', action_id


# Setup publisher for states and subscriber for actions
rospy.init_node('state_machine')
pub = rospy.Publisher('/state', String, queue_size=10)
rospy.Subscriber('/action', String, action_callback, queue_size=10)
rospy.spin()
