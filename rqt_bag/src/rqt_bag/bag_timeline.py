# Software License Agreement (BSD License)
#
# Copyright (c) 2012, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import rospy
import rosbag
import time
import threading

import qt_gui.qt_binding_helper  # @UnusedImport

from QtCore import Qt, QTimer, qWarning
from QtGui import QGraphicsScene, QMessageBox

import bag_helper

from .timeline_frame import TimelineFrame
from .message_listener_thread import MessageListenerThread
from .message_loader_thread import MessageLoaderThread
from .player import Player
from .timeline_menu import TimelinePopupMenu


class BagTimeline(QGraphicsScene):
    """
    """
    def __init__(self, graphicsview, context):
        super(BagTimeline, self).__init__()

        self._bags = []
        self._bag_lock = threading.RLock()

        self.background_task = None
        self.background_task_cancel = False

        self._playhead_lock = threading.RLock()
        self._max_play_speed = 1024.0          # fastest X play speed
        self._min_play_speed = 1.0 / 1024.0    # slowest X play speed
        self._play_speed = 0.0
        self._play_all = False
        self._playhead_positions_cvs = {}
        self._playhead_positions = {}                  # topic -> (bag, position)
        self._message_loaders = {}
        self._messages_cvs = {}
        self._messages = {}                  # topic -> (bag, msg_data)
        self._message_listener_threads = {}                  # listener -> MessageListenerThread
        self._views = []
        self._listeners = {}
        self._player = False
        ## Playing

        self.last_frame = None
        self.last_playhead = None
        self.desired_playhead = None
        self.wrap = True  # should the playhead wrap when it reaches the end?
        self.stick_to_end = False  # should the playhead stick to the end?
        # Trap SIGINT to close the threads

#        def sigint_handler(signum, frame):
#            # TODO verify this doesn't cause problems if we close the plugin and then ctrl-c
#            self._close()
#            sys.exit(0)
#        import signal
#        signal.signal(signal.SIGINT, sigint_handler)

        self._timeline_frame = TimelineFrame(graphicsview)
        self._timeline_frame.setPos(0, 0)
        self.addItem(self._timeline_frame)

        self._play_timer = QTimer()
        self._play_timer.timeout.connect(self.on_idle)
        self._play_timer.start(1)
        self._context = context
        self.popups = set()

    def get_context(self):
        return self._context

    def set_confine_playhead_state(self, confine_to_selection):
        self.play_all = not confine_to_selection

    def set_publishing_state(self, start_publishing):
        if start_publishing:
            for topic in self._timeline_frame.topics:
                if not self.start_publishing(topic):
                    break
        else:
            for topic in self._timeline_frame.topics:
                self.stop_publishing(topic)

    def on_idle(self):
        if self.play_speed != 0.0:
            self._step_playhead()

    def _close(self):
        if self.background_task is not None:
            self.background_task_cancel = True
        self._timeline_frame.close()
        if self._player:
            self._player.stop()
        for bag in self._bags:
            bag.close()

        for view in self._views:
            view.parent.close()

#        if self._recorder:
#            self._recorder.stop()

    def __del__(self):
        if not self.background_task_cancel:
            self._close()

    def add_bag(self, bag):
        self._bags.append(bag)

        bag_topics = bag_helper.get_topics(bag)
#        for i in range(len(bag_topics)):
#            if bag_topics[i][0] != '/':
#                bag_topics[i] = bag_topics[i][1:]

        new_topics = set(bag_topics) - set(self._timeline_frame.topics)

        for topic in new_topics:
            self._playhead_positions_cvs[topic] = threading.Condition()
            self._messages_cvs[topic] = threading.Condition()
            self._message_loaders[topic] = MessageLoaderThread(self, topic)

        self._timeline_frame._start_stamp = self._get_start_stamp()
        self._timeline_frame._end_stamp = self._get_end_stamp()
        self._timeline_frame.topics = self._get_topics()
        self._timeline_frame._topics_by_datatype = self._get_topics_by_datatype()
        # If this is the first bag, reset the timeline
        if self._timeline_frame._stamp_left is None:
            self._timeline_frame.reset_timeline()

        # Invalidate entire index cache for all topics in this bag
        with self._timeline_frame.index_cache_cv:
            for topic in bag_topics:
                self._timeline_frame.invalidated_caches.add(topic)
                if topic in self._timeline_frame.index_cache:
                    del self._timeline_frame.index_cache[topic]

            self._timeline_frame.index_cache_cv.notify()

#TODO fix these _ they are not private
    def _get_start_stamp(self):
        with self._bag_lock:
            start_stamp = None
            for bag in self._bags:
                bag_start_stamp = bag_helper.get_start_stamp(bag)
                if bag_start_stamp is not None and (start_stamp is None or bag_start_stamp < start_stamp):
                    start_stamp = bag_start_stamp
            return start_stamp

    def _get_end_stamp(self):
        with self._bag_lock:
            end_stamp = None
            for bag in self._bags:
                bag_end_stamp = bag_helper.get_end_stamp(bag)
                if bag_end_stamp is not None and (end_stamp is None or bag_end_stamp > end_stamp):
                    end_stamp = bag_end_stamp
            return end_stamp

    def _get_topics(self):
        with self._bag_lock:
            topics = set()
            for bag in self._bags:
                for topic in bag_helper.get_topics(bag):
                    topics.add(topic)
            return sorted(topics)

    def _get_topics_by_datatype(self):
        with self._bag_lock:
            topics_by_datatype = {}
            for bag in self._bags:
                for datatype, topics in bag_helper.get_topics_by_datatype(bag).items():
                    topics_by_datatype.setdefault(datatype, []).extend(topics)
            return topics_by_datatype

    def get_datatype(self, topic):
        with self._bag_lock:
            datatype = None
            for bag in self._bags:
                bag_datatype = bag_helper.get_datatype(bag, topic)
                if datatype and bag_datatype and (bag_datatype != datatype):
                    raise Exception('topic %s has multiple datatypes: %s and %s' % (topic, datatype, bag_datatype))
                datatype = bag_datatype
            return datatype

    def get_entries(self, topics, start_stamp, end_stamp):
        with self._bag_lock:
            from rosbag import bag

            bag_entries = []
            for b in self._bags:
                bag_start_time = bag_helper.get_start_stamp(b)
                if bag_start_time is not None and bag_start_time > end_stamp:
                    continue

                bag_end_time = bag_helper.get_end_stamp(b)
                if bag_end_time is not None and bag_end_time < start_stamp:
                    continue

                connections = list(b._get_connections(topics))
                bag_entries.append(b._get_entries(connections, start_stamp, end_stamp))

            for entry, _ in bag._mergesort(bag_entries, key=lambda entry: entry.time):
                yield entry

    def get_entries_with_bags(self, topic, start_stamp, end_stamp):
        with self._bag_lock:
            from rosbag import bag   # for _mergesort

            bag_entries = []
            bag_by_iter = {}
            for b in self._bags:
                bag_start_time = bag_helper.get_start_stamp(b)
                if bag_start_time is not None and bag_start_time > end_stamp:
                    continue

                bag_end_time = bag_helper.get_end_stamp(b)
                if bag_end_time is not None and bag_end_time < start_stamp:
                    continue

                connections = list(b._get_connections(topic))
                it = iter(b._get_entries(connections, start_stamp, end_stamp))
                bag_by_iter[it] = b
                bag_entries.append(it)

            for entry, it in bag._mergesort(bag_entries, key=lambda entry: entry.time):
                yield bag_by_iter[it], entry

    def get_entry(self, t, topic):
        with self._bag_lock:
            entry_bag, entry = None, None
            for bag in self._bags:
                bag_entry = bag._get_entry(t, bag._get_connections(topic))
                if bag_entry and (not entry or bag_entry.time > entry.time):
                    entry_bag, entry = bag, bag_entry

            return entry_bag, entry

    def get_entry_after(self, t):
        with self._bag_lock:
            entry_bag, entry = None, None
            for bag in self._bags:
                bag_entry = bag._get_entry_after(t, bag._get_connections())
                if bag_entry and (not entry or bag_entry.time < entry.time):
                    entry_bag, entry = bag, bag_entry

            return entry_bag, entry

    def get_next_message_time(self):
        if self._timeline_frame.playhead is None:
            return None

        _, entry = self.get_entry_after(self._timeline_frame.playhead)
        if entry is None:
            return self._timeline_frame._start_stamp

        return entry.time

    ### Copy messages to...

    def start_background_task(self, background_task):
        if self.background_task is not None:
            QMessageBox(QMessageBox.Warning, 'Exclamation', 'Background operation already running:\n\n%s' % self.background_task, QMessageBox.Ok).exec_()
#            dialog.ShowModal()
            return False

        self.background_task = background_task
        self.background_task_cancel = False
        return True

    def stop_background_task(self):
        self.background_task = None

    def copy_region_to_bag(self, filename):
        if len(self._bags) > 0:
            self._export_region(filename, self._timeline_frame.topics, self._timeline_frame.play_region[0], self._timeline_frame.play_region[1])

    def _export_region(self, path, topics, start_stamp, end_stamp):
        if not self.start_background_task('Copying messages to "%s"' % path):
            return
# TODO implement a status bar area with information on the current save status
        bag_entries = list(self.get_entries_with_bags(topics, start_stamp, end_stamp))

        if self.background_task_cancel:
            return

        # Get the total number of messages to copy
        total_messages = len(bag_entries)

        # If no messages, prompt the user and return
        if total_messages == 0:
            QMessageBox(QMessageBox.Warning, 'rxbag', 'No messages found', QMessageBox.Ok).exec_()
            return

        # Open the path for writing
        try:
            export_bag = rosbag.Bag(path, 'w')
        except Exception:
            QMessageBox(QMessageBox.Warning, 'rxbag', 'Error opening bag file [%s] for writing' % path, QMessageBox.Ok).exec_()

        # Run copying in a background thread
        self._export_thread = threading.Thread(target=self._run_export_region, args=(export_bag, topics, start_stamp, end_stamp, bag_entries))
        self._export_thread.start()

    def _run_export_region(self, export_bag, topics, start_stamp, end_stamp, bag_entries):
        total_messages = len(bag_entries)
        update_step = max(1, total_messages / 100)
        message_num = 1
        progress = 0
        # Write out the messages
        for bag, entry in bag_entries:
            if self.background_task_cancel:
                break

            try:
                topic, msg, t = self.read_message(bag, entry.position)
                export_bag.write(topic, msg, t)
            except Exception as ex:
                qWarning('Error exporting message at position %s: %s' % (str(entry.position), str(ex)))
                export_bag.close()
                self.stop_background_task()
                return

            if message_num % update_step == 0 or message_num == total_messages:
                new_progress = int(100.0 * (float(message_num) / total_messages))
                if new_progress != progress:
                    progress = new_progress

            message_num += 1

        # Close the bag
        try:
            export_bag.close()
        except Exception as ex:
            QMessageBox(QMessageBox.Warning, 'rxbag', 'Error closing bag file [%s]: %s' % (export_bag.filename, str(ex)), QMessageBox.Ok).exec_()
        self.stop_background_task()

    def read_message(self, bag, position):
        with self._bag_lock:
            return bag._read_message(position)

    ### Mouse events
    def on_mouse_down(self, event):
        if event.buttons() == Qt.LeftButton:
            self._timeline_frame.on_left_down(event)
        elif event.buttons() == Qt.RightButton:
            TimelinePopupMenu(self, event)

    def on_mouse_up(self, event):
        self._timeline_frame.on_mouse_up(event)

    def on_mouse_move(self, event):
        self._timeline_frame.on_mouse_move(event)

    def on_mousewheel(self, event):
        self._timeline_frame.on_mousewheel(event)

    def zoom_in(self):
        self._timeline_frame.zoom_in()

    def zoom_out(self):
        self._timeline_frame.zoom_out()

    def reset_zoom(self):
        self._timeline_frame.reset_zoom()

    ### Publishing
    def is_publishing(self, topic):
        return self._player and self._player.is_publishing(topic)

    def start_publishing(self, topic):
        if not self._player and not self._create_player():
            return False

        self._player.start_publishing(topic)
        return True

    def stop_publishing(self, topic):
        if not self._player:
            return False

        self._player.stop_publishing(topic)
        return True

    def _create_player(self):
        if not self._player:
            try:
                self._player = Player(self)
            except Exception as ex:
                qWarning('Error starting player; aborting publish: %s' % str(ex))
                return False

        return True

    # property: play_all
    def _get_play_all(self):
        return self._play_all

    def _set_play_all(self, play_all):
        if play_all == self._play_all:
            return

        self._play_all = not self._play_all

        self.last_frame = None
        self.last_playhead = None
        self.desired_playhead = None

    play_all = property(_get_play_all, _set_play_all)

    def toggle_play_all(self):
        self.play_all = not self.play_all

    ### Playing
    def _step_playhead(self):
        # Reset on switch of playing mode
        if self._timeline_frame.playhead != self.last_playhead:
            self.last_frame = None
            self.last_playhead = None
            self.desired_playhead = None

        if self._play_all:
            self.step_next_message()
        else:
            self.step_fixed()

    def step_fixed(self):
        if self.play_speed == 0.0 or not self._timeline_frame.playhead:
            self.last_frame = None
            self.last_playhead = None
            return

        now = rospy.Time.from_sec(time.time())
        if self.last_frame:
            # Get new playhead
            if self.stick_to_end:
                new_playhead = self.end_stamp
            else:
                new_playhead = self._timeline_frame.playhead + rospy.Duration.from_sec((now - self.last_frame).to_sec() * self.play_speed)

                start_stamp, end_stamp = self._timeline_frame.play_region

                if new_playhead > end_stamp:
                    if self.wrap:
                        if self.play_speed > 0.0:
                            new_playhead = start_stamp
                        else:
                            new_playhead = end_stamp
                    else:
                        new_playhead = end_stamp

                        if self.play_speed > 0.0:
                            self.stick_to_end = True

                elif new_playhead < start_stamp:
                    if self.wrap:
                        if self.play_speed < 0.0:
                            new_playhead = end_stamp
                        else:
                            new_playhead = start_stamp
                    else:
                        new_playhead = start_stamp

            # Update the playhead
            self._timeline_frame.playhead = new_playhead

        self.last_frame = now
        self.last_playhead = self._timeline_frame.playhead

    def step_next_message(self):
        if self.play_speed <= 0.0 or not self._timeline_frame.playhead:
            self.last_frame = None
            self.last_playhead = None
            return

        if self.last_frame:
            if not self.desired_playhead:
                self.desired_playhead = self._timeline_frame.playhead
            else:
                delta = rospy.Time.from_sec(time.time()) - self.last_frame
                if delta > rospy.Duration.from_sec(0.1):
                    delta = rospy.Duration.from_sec(0.1)
                self.desired_playhead += delta

            # Get the occurrence of the next message
            next_message_time = self.get_next_message_time()

            if next_message_time < self.desired_playhead:
                self._timeline_frame.playhead = next_message_time
            else:
                self._timeline_frame.playhead = self.desired_playhead

        self.last_frame = rospy.Time.from_sec(time.time())
        self.last_playhead = self._timeline_frame.playhead

    ### Views / listeners
    def add_view(self, topic, view):
        self._views.append(view)
        self.add_listener(topic, view)

    def remove_view(self, topic, view):
        self.remove_listener(topic, view)
        self._views.remove(view)

        self.update()

    def has_listeners(self, topic):
        return topic in self._listeners

    def add_listener(self, topic, listener):
        self._listeners.setdefault(topic, []).append(listener)

        self._message_listener_threads[(topic, listener)] = MessageListenerThread(self, topic, listener)
        # Notify the message listeners
        self._message_loaders[topic].reset()
        with self._playhead_positions_cvs[topic]:
            self._playhead_positions_cvs[topic].notify_all()

        self.update()

    def remove_listener(self, topic, listener):
        topic_listeners = self._listeners.get(topic)
        if topic_listeners is not None and listener in topic_listeners:
            topic_listeners.remove(listener)

            if len(topic_listeners) == 0:
                del self._listeners[topic]

            # Stop the message listener thread
            if (topic, listener) in self._message_listener_threads:
                self._message_listener_threads[(topic, listener)].stop()
                del self._message_listener_threads[(topic, listener)]
            self.update()

    ### Playhead

    # property: play_speed
    def _get_play_speed(self):
        if self._timeline_frame._paused:
            return 0.0
        return self._play_speed

    def _set_play_speed(self, play_speed):
        if play_speed == self._play_speed:
            return

        if play_speed > 0.0:
            self._play_speed = min(self._max_play_speed, max(self._min_play_speed, play_speed))
        elif play_speed < 0.0:
            self._play_speed = max(-self._max_play_speed, min(-self._min_play_speed, play_speed))
        else:
            self._play_speed = play_speed

        if self._play_speed < 1.0:
            self.stick_to_end = False

        self.update()
    play_speed = property(_get_play_speed, _set_play_speed)

    def toggle_play(self):
        if self._play_speed != 0.0:
            self.play_speed = 0.0
        else:
            self.play_speed = 1.0

    def navigate_play(self):
        self.play_speed = 1.0

    def navigate_stop(self):
        self.play_speed = 0.0

    def navigate_rewind(self):
        if self._play_speed < 0.0:
            new_play_speed = self._play_speed * 2.0
        elif self._play_speed == 0.0:
            new_play_speed = -1.0
        else:
            new_play_speed = self._play_speed * 0.5

        self.play_speed = new_play_speed

    def navigate_fastforward(self):
        if self._play_speed > 0.0:
            new_play_speed = self._play_speed * 2.0
        elif self._play_speed == 0.0:
            new_play_speed = 2.0
        else:
            new_play_speed = self._play_speed * 0.5

        self.play_speed = new_play_speed

    def navigate_start(self):
        self._timeline_frame.playhead = self._timeline_frame.play_region[0]

    def navigate_end(self):
        self._timeline_frame.playhead = self._timeline_frame.play_region[1]