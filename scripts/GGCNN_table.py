#!/usr/bin/python

import rospy
import actionlib
import numpy as np
import argparse
import copy
from copy import deepcopy
import rosservice
import sys
import re

from std_msgs.msg import Float64MultiArray, Float32MultiArray
from control_msgs.msg import FollowJointTrajectoryAction, FollowJointTrajectoryGoal
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from geometry_msgs.msg import WrenchStamped
from controller_manager_msgs.srv import SwitchController

# Gazebo
from gazebo_msgs.msg import ModelState, ModelStates, ContactsState, ContactState, LinkState
from gazebo_msgs.srv import GetModelState, GetLinkState

from tf import TransformListener, TransformerROS, TransformBroadcaster
from tf.transformations import euler_from_quaternion, quaternion_from_euler

# Inverse kinematics
from trac_ik_python.trac_ik import IK

# Robotiq
# import roslib; roslib.load_manifest('robotiq_2f_gripper_control')
from robotiq_2f_gripper_control.msg import _Robotiq2FGripper_robot_output  as outputMsg

MOVE_GRIPPER = True
CLOSE_GRIPPER_VEL = 0.05
OPEN_GRIPPER_VEL = -0.1
STOP_GRIPPER_VEL = 0.0
MIN_GRASP_ANGLE = 0.1
MAX_GRASP_ANGLE = 0.70
STARTED_GRIPPER = False
CONTACT = False
MIN_OPEN_INIT = 0.40
MAX_CLOSE_INIT = 0.45
GRIPPER_INIT = True
# GRASPING = True # False

def parse_args():
    parser = argparse.ArgumentParser(description='AAPF_Orientation')
    parser.add_argument('--gazebo', action='store_true', help='Set the parameters related to the simulated enviroonment in Gazebo')
    args = parser.parse_args()
    return args

class vel_control(object):
    def __init__(self, args, joint_values = None):
        rospy.init_node('command_GGCNN_ur5')

        self.args = args
        self.joint_values_home = joint_values

        # Topic used to publish vel commands
        self.pub_vel = rospy.Publisher('/joint_group_vel_controller/command', Float64MultiArray,  queue_size=1)

        self.joint_vels_gripper = Float64MultiArray()
        self.pub_vel_gripper = rospy.Publisher('/gripper_controller_vel/command', Float64MultiArray,  queue_size=1)

        # Used to perform TF transformations
        self.tf = TransformListener()

        # Used to change the controller
        self.controller_switch = rospy.ServiceProxy('/controller_manager/switch_controller', SwitchController)

        # actionClient used to send joint positions
        self.client = actionlib.SimpleActionClient('pos_based_pos_traj_controller/follow_joint_trajectory', FollowJointTrajectoryAction)
        print "Waiting for server (pos_based_pos_traj_controller)..."
        self.client.wait_for_server()
        print "Connected to server (pos_based_pos_traj_controller)"
        
        self.initial_traj_duration = 5.0
        self.final_traj_duration = 1000.0

        # Gazebo topics
        if self.args.gazebo:
            # Subscriber used to read joint values
            rospy.Subscriber('/joint_states', JointState, self.ur5_actual_position, queue_size=2)
            rospy.sleep(2.0)
            self.pub_model = rospy.Publisher('/gazebo/set_link_state', LinkState, queue_size=1)
            self.model = rospy.wait_for_message('gazebo/model_states', ModelStates)
            self.model_coordinates = rospy.ServiceProxy( '/gazebo/get_link_state', GetLinkState)
            rospy.Subscriber('/ft_sensor/raw', WrenchStamped, self.monitor_wrench, queue_size=1)

            # LEFT GRIPPER
            rospy.Subscriber('/left_finger_bumper_vals', ContactsState, self.monitor_contacts_left_finger) # ContactState
            self.left_status = False
            self.contactState_left = ContactState()
            
        # GGCNN
        self.joint_values_ggcnn = []
        self.posCB = []
        self.ori = []
        rospy.Subscriber('ggcnn/out/command', Float32MultiArray, self.ggcnn_command, queue_size=10)

        # Standard attributes used to send joint position commands
        self.joint_vels = Float64MultiArray()
        self.goal = FollowJointTrajectoryGoal()
        self.goal.trajectory = JointTrajectory()
        self.goal.trajectory.joint_names = ['shoulder_pan_joint', 'shoulder_lift_joint',
                                            'elbow_joint', 'wrist_1_joint', 'wrist_2_joint',
                                            'wrist_3_joint']
        self.initial_time = 4

        # Robotiq control
        self.pub_gripper_command = rospy.Publisher('Robotiq2FGripperRobotOutput', outputMsg.Robotiq2FGripper_robot_output, queue_size=1)
        self.d = None # msg received from GGCN

        # Denavit-Hartenberg parameters of UR5
        # The order of the parameters is d1, SO, EO, a2, a3, d4, d45, d5, d6
        self.ur5_param = (0.089159, 0.13585, -0.1197, 0.425, 0.39225, 0.10915, 0.093, 0.09465, 0.0823 + 0.15)

    def turn_velocity_controller_on(self):
        self.controller_switch('joint_group_vel_controller', 'pos_based_pos_traj_controller', 1)
        
    def turn_position_controller_on(self):
        self.controller_switch('pos_based_pos_traj_controller', 'joint_group_vel_controller', 1)

    def monitor_contacts_left_finger(self, msg):
        if msg.states:
            string = msg.states[0].collision1_name
            string = re.findall(r'::(.+?)::',string)[0]
            self.left_status = True
            print(string)
        else:
            self.left_status = False

        print(self.left_status)

    def get_ik(self, pose, ori = 'pick'):
        """Get the inverse kinematics 
        
        Get the inverse kinematics of the UR5 robot using track_IK package giving a desired intial pose
        
        Arguments:
            pose {list} -- A pose representing x, y and z
        
        Keyword Arguments:
            ori {str} -- Define the final orientation of the robot (default: {'pick'})
        
        Returns:
            {list} -- Joint angles or None if track_ik is not able to find a valid solution
        """
        grasping_link_offset = 0.15
        if ori == 'place':
            q = quaternion_from_euler(0.0, -3.14, 0.0)
            # Joint order:
            # ('shoulder_link', 'upper_arm_link', 'forearm_link', 'wrist_1_link', 'wrist_2_link', 'wrist_3_link', 'tool0')
            
            ik_solver = IK("base_link", "tool0", solve_type="Distance")
            sol = ik_solver.get_ik([0.2201039360819781, -1.573845095552878, -1.521853400505349, -1.6151347051274518, 1.5704492904506875, 0.0], 
                    pose[0], pose[1], pose[2] + grasping_link_offset, q[0], q[1], q[2], q[3])
            if sol is not None:
                sol = list(sol)
                sol[-1] = 0.0
            
        elif ori =='pick':
            q = quaternion_from_euler(0.0, -1.57, 0.0)
            ik_solver = IK("base_link", "tool0", solve_type="Distance")
            sol = ik_solver.get_ik([0.30138727537716037, -1.6153495734651055, -2.086529024901576, 0.5611198563245517, 1.2694091499722355, 0.0], 
                pose[0] + grasping_link_offset, pose[1], pose[2], q[0], q[1], q[2], q[3])
            if sol is not None:
                sol = list(sol)
                sol[-1] = 0.0
    
        return sol


    """
    Quintic polynomial trajectory
    """
    def traj_planner(self, cart_pos, movement, ggcnn = False, way_points_number = 10):
        """
        pp - Position points
        vp - Velocity points
        """
        if ggcnn:
            cart_pos = self.posCB

        v0 = a0 = vf = af = 0
        t0 = self.initial_traj_duration
        tf = (t0 + self.final_traj_duration) / way_points_number # tf by way point
        t = tf / 10 # for each movement
        ta = tf / 10 # to complete each movement
        a = [0.0]*6
        pos_points, vel_points, acc_points = [0.0]*6, [0.0]*6, [0.0]*6
        joint_pos = self.get_ik(cart_pos, movement)

        if ggcnn:
            joint_pos[-1] = 0.0 # FOR TEST GGCNN

        goal = FollowJointTrajectoryGoal()
        goal.trajectory = JointTrajectory()
        goal.trajectory.joint_names = ['shoulder_pan_joint', 'shoulder_lift_joint',
                                            'elbow_joint', 'wrist_1_joint', 'wrist_2_joint',
                                            'wrist_3_joint']

        for i in range(6):
            q0 = self.actual_position[i]
            qf = joint_pos[i]

            b = np.array([q0,v0,a0,qf,vf,af]).transpose()
            m = np.array([[1, t0, t0**2,   t0**3,    t0**4,    t0**5],
                          [0,  1,  2*t0, 3*t0**2,  4*t0**3,  5*t0**4],
                          [0,  0,     2,    6*t0, 12*t0**2, 20*t0**3],
                          [1, tf, tf**2,   tf**3,    tf**4,    tf**5],
                          [0,  1,  2*tf, 3*tf**2,  4*tf**3,  5*tf**4],
                          [0,  0,     2,    6*tf, 12*tf**2, 20*tf**3]])
            a[i] = np.linalg.inv(m).dot(b)

        for i in range(way_points_number):
            for j in range(6):
                pos_points[j] =   a[j][0] +   a[j][1]*t +    a[j][2]*t**2 +    a[j][3]*t**3 +   a[j][4]*t**4 + a[j][5]*t**5
                vel_points[j] =   a[j][1] + 2*a[j][2]*t +  3*a[j][3]*t**2 +  4*a[j][4]*t**3 + 5*a[j][5]*t**4
                acc_points[j] = 2*a[j][2] + 6*a[j][3]*t + 12*a[j][4]*t**2 + 20*a[j][5]*t**3

            goal.trajectory.points.append(JointTrajectoryPoint(positions = pos_points,
                                                               velocities = vel_points,
                                                               accelerations = acc_points,
                                                               time_from_start = rospy.Duration(t))) #default 0.1*i + 5
            t += ta

        self.client.send_goal(goal)
        # self.client.wait_for_result()

    """
    GGCNN Command Subscriber Callback
    """
    def ggcnn_command(self, msg):
        # msg = rospy.wait_for_message('/ggcnn/out/command', Float32MultiArray)
        self.tf.waitForTransform("object_link", "base_link", rospy.Time(), rospy.Duration(4.0))
        self.tf.waitForTransform("camera_depth_optical_frame", "object_detected", rospy.Time(), rospy.Duration(4.0))
        self.d = list(msg.data)
        # posCB is the position of the object frame related to the base_link
        self.posCB, _ = self.tf.lookupTransform("base_link", "object_link", rospy.Time())
        _, oriObjCam = self.tf.lookupTransform("camera_depth_optical_frame", "object_detected", rospy.Time())
        self.ori = euler_from_quaternion(oriObjCam)
        print(self.posCB)

        
    def genCommand(self, char, command, pos = None):
        """Update the command according to the character entered by the user."""    

        if char == 'a':
            # command = outputMsg.Robotiq2FGripper_robot_output();
            command.rACT = 1 # Gripper activation
            command.rGTO = 1 # Go to position request
            command.rSP  = 255 # Speed
            command.rFR  = 150 # Force

        if char == 'r':
            command.rACT = 0

        if char == 'c':
            command.rACT = 1
            command.rGTO = 1
            command.rATR = 0
            command.rPR = 255
            command.rSP = 40
            command.rFR = 150
            
        # @param pos Gripper width in meters. [0, 0.087]
        if char == 'p':
            command.rACT = 1
            command.rGTO = 1
            command.rATR = 0
            command.rPR = int(np.clip((13.-230.)/0.14 * self.d[-2] + 230., 0, 255))
            command.rSP = 40
            command.rFR = 150

        if char == 'o':
            command.rACT = 1
            command.rGTO = 1
            command.rATR = 0
            command.rPR = 0
            command.rSP = 40
            command.rFR = 150

        return command

    def command_gripper(self, action):
        command = outputMsg.Robotiq2FGripper_robot_output();
        command = self.genCommand(action, command)
        self.pub_gripper_command.publish(command)  

    """
    This method check if the goal position was reached
    """
    def all_close(self, goal, tolerance = 0.015):
        error = np.sum([(self.actual_position[i] - goal[i])**2 for i in range(6)])
        if error > tolerance:
            return False

        return True

    """
    This method monitor the force applied to the gripper
    """       
    def monitor_wrench(self, msg):
        global MOVE_GRIPPER, STARTED_GRIPPER, CONTACT, GRASPING

        # print(msg)
        if STARTED_GRIPPER:
            if float(msg.wrench.force.x) < -2.0 or float(msg.wrench.force.x) > 2.0 or \
               float(msg.wrench.force.y) < -5.0 or float(msg.wrench.force.y) > 15.0 or \
               float(msg.wrench.force.z) < -4.0 or float(msg.wrench.force.z) > 5.0:
                MOVE_GRIPPER = False
                CONTACT = True
            
    def gripper_init(self):
        global CLOSE_GRIPPER_VEL, OPEN_GRIPPER_VEL
        global MIN_OPEN_INIT, MAX_CLOSE_INIT

        gripper_vel = OPEN_GRIPPER_VEL

        if self.robotic < MIN_OPEN_INIT: # MIN_OPEN_INIT = 0.15
            gripper_vel = CLOSE_GRIPPER_VEL
             
        self.joint_vels_gripper.data = np.array([gripper_vel])
        self.pub_vel_gripper.publish(self.joint_vels_gripper)

        rate = rospy.Rate(125)
        while not rospy.is_shutdown():
            if gripper_vel == OPEN_GRIPPER_VEL:
                if self.robotic < MIN_OPEN_INIT:
                    break
            else:
                if self.robotic > MAX_CLOSE_INIT:
                    break
            rate.sleep()

        print("self.robotic: ", self.robotic)

        self.joint_vels_gripper.data = np.array([0.0])
        self.pub_vel_gripper.publish(self.joint_vels_gripper)

    """
    Control the gripper by using velocity controller
    """
    def gripper_vel_control(self, action):
        global MOVE_GRIPPER

        rate = rospy.Rate(125)

        if action == 'open':
            print(np.array(OPEN_GRIPPER_VEL))
            self.joint_vels_gripper.data = np.array([OPEN_GRIPPER_VEL])
            self.pub_vel_gripper.publish(self.joint_vels_gripper)
            while not rospy.is_shutdown() and MOVE_GRIPPER and self.robotic > 0.1:
                rate.sleep()
        elif action == 'close':
            self.joint_vels_gripper.data = np.array([CLOSE_GRIPPER_VEL])
            self.pub_vel_gripper.publish(self.joint_vels_gripper)
            while not rospy.is_shutdown() and MOVE_GRIPPER and self.robotic < 0.6:
                rate.sleep()
           
        # stops the robot after the goal is reached
        rospy.loginfo("Gripper stopped!")
        self.joint_vels_gripper.data = np.array([0.0])
        self.pub_vel_gripper.publish(self.joint_vels_gripper)

    """
    The joint states published by /joint_staes of the UR5 robot are in wrong order.
    /joint_states topic normally publishes the joint in the following order:
    [elbow_joint, shoulder_lift_joint, shoulder_pan_joint, wrist_1_joint, wrist_2_joint, wrist_3_joint]
    But the correct order of the joints that must be sent to the robot is:
    ['shoulder_pan_joint', 'shoulder_lift_joint', 'elbow_joint', 'wrist_1_joint', 'wrist_2_joint', 'wrist_3_joint']
    """
    def ur5_actual_position(self, joint_values_from_ur5):
        if self.args.gazebo:
            self.th3, self.robotic, self.th2, self.th1, self.th4, self.th5, self.th6 = joint_values_from_ur5.position
            # print("Robotic angle: ", self.robotic)
        else:
            self.th3, self.th2, self.th1, self.th4, self.th5, self.th6 = joint_values_from_ur5.position
        
        self.actual_position = [self.th1, self.th2, self.th3, self.th4, self.th5, self.th6]
        
  
    """
    Send the HOME position to the robot
    self.client.wait_for_result() won't work well in Gazebo.
    Instead, a while loop has been created to ensure that the robot reaches the
    goal even after the failure.
    In order to avoid the gripper to keep moving after the node is killed, the method gripper_init() is also called
    """
    def home_pos(self):
        global GRIPPER_INIT
        self.turn_position_controller_on()
        rospy.sleep(0.1)

        if GRIPPER_INIT and self.args.gazebo:
            self.gripper_init()

        # First point is current position
        try:
            self.goal.trajectory.points = [(JointTrajectoryPoint(positions=self.joint_values_home, velocities=[0]*6, time_from_start=rospy.Duration(self.initial_traj_duration)))]
            self.client.send_goal(self.goal)
            self.client.wait_for_result()
        except KeyboardInterrupt:
            self.client.cancel_goal()
            raise
        except:
            raise

        print "\n==== Goal reached!"

    """
    This method was created because the real robot always reach the correct 
    position but it is not always true for gazebo
    """
    def set_pos_robot(self, joint_values_param, time = 18.0):
        rospy.sleep(0.1)
        self.turn_position_controller_on()

        joint_values = deepcopy(joint_values_param)

        # First point is current position
        try:
            self.goal.trajectory.points = [(JointTrajectoryPoint(positions=joint_values, velocities=[0]*6, time_from_start=rospy.Duration(time)))]
            if self.args.gazebo:
                if not self.all_close(joint_values):
                    self.client.send_goal(self.goal)
                    self.client.wait_for_result()
                    while not self.all_close(joint_values):
                        self.client.send_goal(self.goal)
                        self.client.wait_for_result()
            else:
                self.client.send_goal(self.goal)
                self.client.wait_for_result()
        except KeyboardInterrupt:
            self.client.cancel_goal()
            raise
        except:
            raise

        print "\n==== Goal reached!"

def main():
    global MOVE_GRIPPER, STARTED_GRIPPER, GRIPPER_INIT, GRASPING

    arg = parse_args()

    # Turn position controller ON
    ur5_vel = vel_control(arg)
    ur5_vel.turn_position_controller_on()
    point_init = [-0.3897482470295059, 0.0013201868456541224, 0.02555623365136256] #[-0.40, 0.0, 0.15]
    joint_values_home = ur5_vel.get_ik(point_init, 'place')
    ur5_vel.joint_values_home = joint_values_home

    # Send the robot to the custom HOME position
    raw_input("==== Press enter to 'home' the robot!")
    rospy.on_shutdown(ur5_vel.home_pos)
    ur5_vel.set_pos_robot(ur5_vel.joint_values_home)
    
    raw_input("==== Press enter to init to gripper!")
    if arg.gazebo:
        rospy.loginfo("Starting the gripper in Gazebo! Please wait...")
        ur5_vel.gripper_init()
        rospy.loginfo("Gripper started!")        
    else:
        rospy.loginfo("Starting the real gripper! Please wait...")
        ur5_vel.command_gripper('r')
        rospy.sleep(0.5)
        ur5_vel.command_gripper('a')
        ur5_vel.command_gripper('o')
    
    GRIPPER_INIT = False

    while not rospy.is_shutdown():

        point_test = [-0.3897482470295059, 0.0013201868456541224, 0.02555623365136256]
        raw_input("==== Press enter to move the robot to the pre-grasp position!")
        # Ponto cartesiano que funciona na SSD512 [-0.65, 0.0, 0.20]
        ur5_vel.traj_planner(point_test, 'place', False)

        point_test = [-0.52, 0.2, 0.02555623365136256]
        raw_input("==== Press enter to move the robot to the pre-grasp position!")
        # Ponto cartesiano que funciona na SSD512 [-0.65, 0.0, 0.20]
        ur5_vel.traj_planner(point_test, 'place', False)
                
        # It will be replaced by the GGCNN position
        # It is just to simulate the final position
        # raw_input("==== Press enter to move the robot to the grasp position!")
        # ur5_vel.traj_planner([-0.75, -0.01, 0.20], 'pick')  
                        
        # !!! GGCNN is not yet implemented to pick a object in the printer (makerbot)
        #It closes the gripper before approaching the object
        #It prevents the gripper to collide with other objects when grasping
        if not arg.gazebo:
            raw_input("==== Press enter to close the gripper to a pre-grasp position!")
            ur5_vel.command_gripper('p')

        raw_input("==== Press enter to move the robot to the goal position given by GGCNN!")
        ur5_vel.traj_planner([], 'place', True)

        # As the object iteraction in Gazebo is not working properly, the gripper only closes
        # when using the real robot
        if not arg.gazebo:
            raw_input("==== Press enter to close the gripper!")
            print(ur5_vel.d[-2])
            ur5_vel.command_gripper('c')

        # Move the robot to put the object down after the robot is move backwards from the printer
        # Need to be updated
        # raw_input("==== Press enter to put the object down!")
        # ur5_vel.traj_planner([-0.6, 0.0, 0.25], 'place')   

        # raw_input("==== Press enter to open the gripper!")
        # if arg.gazebo:
        #     MOVE_GRIPPER = True    
        #     ur5_vel.gripper_vel_control('open')
        # else:
        #     ur5_vel.command_gripper('o')

if __name__ == '__main__':
    try:
        main()
    except rospy.ROSInterruptException:
	    print "Program interrupted before completion"