#!/usr/bin/env python

import json
import cv2
import os
import subprocess
import rospy
import numpy as np
from threading import Thread
from Queue import Queue
from datetime import datetime
from std_msgs.msg import String
from state_machine import states, actions
from camera_modules import change_util
from camera_modules.change_util import crop_to_table, convert_to_grey, calculate_contours, save_frame, draw_changes
from util_modules import config_access
from interaction_modules.reporting_modules import email_report

if __name__ != '__main__':
    from sys import stderr
    # No one should import this code (stops multiple identical nodes being started)
    print >> stderr, __name__, 'should not be imported!'
    exit(1)

# Frame information
FRAME_QUEUE_SIZE = config_access.get_config(config_access.KEY_CHANGE_FRAME_QUEUE_SIZE)
previous_frames = Queue(maxsize=FRAME_QUEUE_SIZE)
change_history = Queue(maxsize=FRAME_QUEUE_SIZE)
previous_contours = []

# Change detection parameters
MIN_CONTOUR_AREA = config_access.get_config(config_access.KEY_CHANGE_MIN_CONTOUR_AREA)
CHANGE_SIGNIFICANT_CHANGE_THRESHOLD = config_access.get_config(config_access.KEY_CHANGE_SIGNIFICANT_CHANGE_THRESHOLD)

# State information
running = False
state_id = None
state_data = None

thief_went_right = True


def detect_change(original_image):
    """
    Detects change between the current image and the last.
    Some code inspired by https://gist.github.com/rrama/0bd1c29c8a1c1597b1eaf63847cecbf2
    :param original_image: The image to compare to the previous (the method will return False if it is the first call)
    :type original_image: numpy.ndarray
    :return: True is change was detected, False if not
    :rtype: bool
    """


    global previous_frames
    global state_data
    global pub
    global MIN_CONTOUR_AREA
    global previous_contours

    image = crop_to_table(original_image)

    grey = convert_to_grey(image)

    # If the queue isn't full, add this frame to the queue and say 'no changes'
    if not previous_frames.full():
        previous_frames.put(grey)
        return False
    elif state_id == states.LOCKING:
        # Now locked, say so
        pub.publish(json.dumps({'id': actions.READY_TO_LOCK, 'data': state_data}))

    contours = calculate_contours(grey, previous_frames.get())

    # decide_which_way(contours, original_image)
    # Has there been a change?
    changed = False
    saved_countour_list = []
    for contour in contours:
        # If the contour is too small, ignore it
        if cv2.contourArea(contour) < MIN_CONTOUR_AREA:
            continue
        else:
            saved_countour_list.append(contour)
            changed = True
            break

    # Draw the changes
    draw_changes(image, contours)

    # Save the most recent frame
    previous_frames.put(grey)

    # Add timestamp
    vertical, _ = original_image.shape[:2]
    cv2.putText(original_image, str(datetime.utcnow()), (0, vertical), cv2.FONT_HERSHEY_PLAIN, 1, (0, 0, 255), 1)

    # Save the image if there was change
    if changed:
        save_frame(original_image)

    previous_contours.append(saved_countour_list)
    if(len(previous_contours) > FRAME_QUEUE_SIZE):
        previous_contours = previous_contours[1:]

    return changed


def detect_color(image, contours):
    avg_b = 0
    avg_g = 0
    avg_r = 0
    total = 0
    for contour in contours:
        if(len(contour) > 0):
            x, y, w, h = cv2.boundingRect(contour)
            for i in range(x, x+w-1):
                for j in range(y, y+h-1):
                    avg_b += image[j][i][0]
                    avg_g += image[j][i][1]
                    avg_r += image[j][i][2]
                    total += 1
        return (avg_b/total, avg_g/total, avg_r/total)
    return None

def decide_which_way(contours, img):
    global thief_went_right
    if(len(contours) > 0):
        vertical, the_width = img.shape[:2]
        all_x = 0
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            all_x += x

        average_width = all_x / len(contours)
        if average_width < the_width/2:
            thief_went_right = False
        else:
            thief_went_right = True


def detect_significant_change(original_image):
    """
    Using detect_change(), decides if there has been a significant change in recent history
    :param original_image: The image to compare to the previous (the method will return False if it is the first call)
    :type original_image: numpy.ndarray
    :return: True if there has been consistent changes recently, False if not (i.e. no need to alarm)
    :rtype: bool
    """
    global change_history
    global previous_contours
    global CHANGE_SIGNIFICANT_CHANGE_THRESHOLD

    # Find out if there is 'change' in the current frame
    changed = detect_change(original_image)
    # Save if there was a change on this frame
    change_history.put(changed)
    # We need to have at least the queue full before we do the rest of this
    if not change_history.full():
        return False, None
    # Remove the oldest change history
    change_history.get()
    # Decide if we need to return a significant change
    change_history_detected_count = 0
    for _ in range(FRAME_QUEUE_SIZE):
        change_history_change = change_history.get()
        if change_history_change:
            change_history_detected_count += 1
        change_history.put(change_history_change)

    alarm = change_history_detected_count > (FRAME_QUEUE_SIZE * CHANGE_SIGNIFICANT_CHANGE_THRESHOLD)

    decided_colour = None
    if alarm == True:
        avg_b = 0
        avg_g = 0
        avg_r = 0
        total = 0
        for countours in previous_contours:
            if(len(countours) > 0):
                (b,g,r) = detect_color(original_image, countours)
                avg_b += b
                avg_g += g
                avg_r += r
                total += 1
        decided_colour = (avg_b/total, avg_g/total, avg_r/total)

    # If over half of the recent change detection is 'change detected', then we should return a significant change
    return alarm, decided_colour


def reset(cap):
    """
    Resets all configured values to null and handles video file compress and reporting if an alarm was triggered
    :param cap: The frame capture object
    :type cap: VideoCapture
    """
    global previous_frames
    global state_id
    global change_history

    # Release capture and destroy windows
    cap.release()
    cv2.destroyAllWindows()

    change_util.crop_left = None
    change_util.crop_right = None
    previous_frames = Queue(maxsize=FRAME_QUEUE_SIZE)
    change_history = Queue(maxsize=FRAME_QUEUE_SIZE)
    previous_contours = Queue(maxsize=FRAME_QUEUE_SIZE)

    if change_util.video_writer is not None:
        change_util.video_writer.release()
        change_util.video_writer = None

    if change_util.video_file is not None:
        # Compress the video
        dev_null = open(os.devnull, 'w')
        compress_result = subprocess.call(
            ['HandBrakeCLI', '-i', change_util.video_file, '-o', change_util.video_file.replace('.avi', '.mp4'), '-e',
             'x265', '-q', '20'], stdout=dev_null, stderr=subprocess.STDOUT)

        if compress_result == 0:
            # Remove old file
            os.remove(change_util.video_file)

            # We know the video is compressed, and not being written to, so we can send it here
            email_report.send_report_email(state_data['notify_owner'], change_util.video_file.replace('.avi', '.mp4'))
        else:
            # Compression failed, try with the other video anyway
            email_report.send_report_email(state_data['notify_owner'], change_util.video_file)

        change_util.video_file = None


def run():
    """
    Runs the change detector code (finding table, watching table, sending alerts, clean up)
    """
    global running
    global pub
    global state_id
    global state_data

    cap = cv2.VideoCapture(1)

    running = True
    while running:
        # Get the current frame
        ret, frame = cap.read()

        # Apply the method
        changed, decided_colour = detect_significant_change(frame)

        print changed, decided_colour
        if changed and state_id == states.LOCKED_AND_WAITING:
            # publish alarm
            state_data['which_way'] = thief_went_right
            state_data['colour'] = decided_colour
            pub.publish(json.dumps({'id': actions.MOVEMENT_DETECTED, 'data': state_data}))

        cv2.imshow('Table View', frame)
        cv2.waitKey(1)

    # No longer running, reset
    reset(cap)


def callback(state_msg):
    """
    Processes updates to state information
    :param state_msg: The new state information
    :type state_msg: std_msgs.msg.String
    """
    global running
    global state_id
    global state_data
    state_json = json.loads(state_msg.data)
    state_id = state_json['id']
    state_data = state_json['data']

    print state_json
    if state_id == states.LOCKING:
        print 'Locking'
        # Start finding table in a new thread...
        Thread(target=run).start()
    elif state_id == states.LOCKED_AND_WAITING or state_id == states.ALARM:
        # Do nothing (continue running)
        pass
    else:
        # Stop running (code will automatically clean up)
        running = False


# ROS node stuff
rospy.init_node('change_node')
rospy.Subscriber('/state', String, callback, queue_size=10)
pub = rospy.Publisher('/action', String, queue_size=10)

# TESTING
# callback(String(json.dumps({'id': states.LOCKING, 'data': {'current_owner': 'Seb'}})))

rospy.spin()
