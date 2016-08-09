
import rospy
from sensor_msgs.msg import LaserScan
from geometry_msgs.msg import Twist, PoseStamped

import actionlib
from move_base_msgs.msg import MoveBaseGoal, MoveBaseAction, MoveBaseFeedback

import tf

import threading, time
import os

from tensorflow_wrapper import TensorflowWrapper

class DeepMotionPlanner():
    """Use a deep neural network for motion planning"""
    def __init__(self):

        self.target_pose = None
        self.last_scan = None
        self.freq = 25.0

        # Load various ROS parameters
        if not rospy.has_param('~model_path'):
            rospy.logerr('Missing parameter: ~model_path')
            exit()

        self.laser_scan_stride = rospy.get_param('~laser_scan_stride', 2) # Take every ith element
        self.n_laser_scans = rospy.get_param('~n_laser_scans', 540) # Cut n elements from the center to adjust the length
        self.model_path = rospy.get_param('~model_path')
        self.protobuf_file = rospy.get_param('~protobuf_file', 'graph.pb')
        self.use_checkpoints = rospy.get_param('~use_checkpoints', False)
        if not os.path.exists(self.model_path):
            rospy.logerr('Model path does not exist: {}'.format(self.model_path))
            rospy.logerr('Please check the parameter: {}'.format(rospy.resolve_name('~model_path')))
            exit()
        if not os.path.exists(os.path.join(self.model_path, self.protobuf_file)):
            rospy.logerr('Protobuf file does not exist: {}'.format(os.path.join(self.model_path, self.protobuf_file)))
            rospy.logerr('Please check the parameter: {}'.format(rospy.resolve_name('~protobuf_file')))
            exit()
       
        # ROS topics
        scan_sub = rospy.Subscriber('scan', LaserScan, self.scan_callback)
        goal_sub = rospy.Subscriber('/move_base_simple/goal', PoseStamped, self.goal_topic_callback)
        self.cmd_pub  = rospy.Publisher('/cmd_vel', Twist, queue_size=1)

        # We over the same action api as the move base package
        self._as = actionlib.SimpleActionServer('deep_move_base',
                MoveBaseAction, auto_start =
                False)
        self._as.register_goal_callback(self.goal_callback)
        self._as.register_preempt_callback(self.preempt_callback)

        self.transform_listener = tf.TransformListener()

        # Use a separate thread to process the received data
        self.interrupt_event = threading.Event()
        self.processing_thread = threading.Thread(target=self.processing_data)

        self.processing_thread.start()
        self._as.start()
            
        self.navigation_client = actionlib.SimpleActionClient('deep_move_base', MoveBaseAction)
        while not self.navigation_client.wait_for_server(rospy.Duration(5)):
            rospy.loginfo('Waiting for deep_move_base action server')

    def __enter__(self):
        return self
      
    def __exit__(self, exc_type, exc_value, traceback):
        # Make sure to stop the thread properly
        self.interrupt_event.set()
        self.processing_thread.join()

    def scan_callback(self, data):
        """
        Callback function for the laser scan messages
        """
        self.last_scan = data

    def processing_data(self):
        """
        Process the received sensor data and publish a new command

        The function does not return, it is used as thread function and
        runs until the interrupt_event is set
        """
        # Get a handle for the tensorflow interface
        with TensorflowWrapper(self.model_path, protobuf_file=self.protobuf_file, use_checkpoints=self.use_checkpoints) as tf_wrapper:
            next_call = time.time()
            # Stop if the interrupt is requested
            while not self.interrupt_event.is_set():

                # Run processing with the correct frequency
                next_call = next_call+1.0/self.freq
                sleep_time = next_call - time.time()
                if sleep_time > 0.0:
                    time.sleep(sleep_time)
                else:
                    rospy.logerr('Missed control loop frequency')

                # Make sure, we have goal
                if not self._as.is_active():
                    continue
                # Make sure, we received the first laser scan message
                if not self.target_pose or not self.last_scan:
                    continue
                # Get the relative target pose
                target = self.compute_relative_target()
                if not target:
                    continue
                        
                # Prepare the input vector, perform the inference on the model 
                # and publish a new command
                scans = list(self.last_scan.ranges[::self.laser_scan_stride])
                cut_n_elements = (len(scans) - self.n_laser_scans) // 2
                cropped_scans = scans
                if cut_n_elements > 0:
                  rospy.logdebug("Cutting input vector by {0} elements on each side.".format(cut_n_elements))
                  cropped_scans = scans[cut_n_elements:-cut_n_elements]
                if len(cropped_scans)==self.n_laser_scans+1:
                  rospy.logdebug("Input vector has one scan too much. Cutting off last one.")
                  cropped_scans = cropped_scans[0:-1]
                
                input_data = cropped_scans + list(target)

                linear_x, angular_z = tf_wrapper.inference(input_data)

                cmd = Twist()
                cmd.linear.x = linear_x
                cmd.angular.z = angular_z
                self.cmd_pub.publish(cmd)

                # Check if the goal pose is reached
                self.check_goal_reached(target)

    def check_goal_reached(self, target):
        """
        Check if the position and orientation are close enough to the target.
        If this is the case, set the current goal to succeeded.
        """
        position_tolerance = 0.1
        orientation_tolerance = 0.1
        if abs(target[0]) < position_tolerance \
                and abs(target[1]) < position_tolerance \
                and abs(target[2]) < orientation_tolerance:
            self._as.set_succeeded()

    def compute_relative_target(self):
        """
        Compute the target pose in the base_link frame and publish the current pose of the robot
        """
        try:
            # Get the base_link transformation
            (base_position,base_orientation) = self.transform_listener.lookupTransform('/map', '/base_link',
                                                                    rospy.Time())
        except (tf.LookupException, tf.ConnectivityException,
                        tf.ExtrapolationException):
            return None

        # Publish feedback (the current pose)
        feedback = MoveBaseFeedback()
        feedback.base_position.header.stamp = rospy.Time().now()
        feedback.base_position.pose.position.x = base_position[0]
        feedback.base_position.pose.position.y = base_position[1]
        feedback.base_position.pose.position.z = base_position[2]
        feedback.base_position.pose.orientation.x = base_orientation[0]
        feedback.base_position.pose.orientation.y = base_orientation[1]
        feedback.base_position.pose.orientation.z = base_orientation[2]
        feedback.base_position.pose.orientation.w = base_orientation[3]
        self._as.publish_feedback(feedback)

        # Compute the relative goal position
        goal_position_difference = [self.target_pose.target_pose.pose.position.x - feedback.base_position.pose.position.x,
                                    self.target_pose.target_pose.pose.position.y - feedback.base_position.pose.position.y]

        # Get the current orientation and the goal orientation
        current_orientation = feedback.base_position.pose.orientation
        p = [current_orientation.x, current_orientation.y, current_orientation.z, \
                current_orientation.w]
        goal_orientation = self.target_pose.target_pose.pose.orientation
        q = [goal_orientation.x, goal_orientation.y, goal_orientation.z, \
                goal_orientation.w]

        # Rotate the relative goal position into the base frame
        goal_position_base_frame = tf.transformations.quaternion_multiply(
                tf.transformations.quaternion_inverse(p),
                tf.transformations.quaternion_multiply([goal_position_difference[0],
                    goal_position_difference[1], 0, 0], p))

        # Compute the difference to the goal orientation
        orientation_to_target = tf.transformations.quaternion_multiply(q, \
                tf.transformations.quaternion_inverse(p))
        yaw = tf.transformations.euler_from_quaternion(orientation_to_target)[2]

        return (goal_position_base_frame[0], -goal_position_base_frame[1], yaw)

    def goal_callback(self):
        """
        Callback function when a new goal pose is requested
        """
        goal = self._as.accept_new_goal()
        self.target_pose = goal

    def preempt_callback(self):
        """
        Callback function when the current action is preempted
        """
        rospy.logerr('Action preempted')
        self._as.set_preempted(result=None, text='External preemption')

    def goal_topic_callback(self, data):
        # Generate a action message
        goal = MoveBaseGoal()

        goal.target_pose.header.frame_id = 'map'
        goal.target_pose.pose.position.x = data.pose.position.x
        goal.target_pose.pose.position.y = data.pose.position.y


        goal.target_pose.pose.orientation.x = data.pose.orientation.x
        goal.target_pose.pose.orientation.y = data.pose.orientation.y
        goal.target_pose.pose.orientation.z = data.pose.orientation.z
        goal.target_pose.pose.orientation.w = data.pose.orientation.w

        # Send the waypoint
        self.navigation_client.send_goal(goal)

