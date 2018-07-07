cmake_minimum_required(VERSION 2.8.3)
project(rosqc)

find_package(catkin REQUIRED COMPONENTS rosnode rospy)

catkin_python_setup()

catkin_package()
