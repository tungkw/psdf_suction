#!/usr/bin/python3
import os
from turtle import shape
import numpy as np
import json
import torch
from scipy.spatial.transform.rotation import Rotation as R
from skimage import measure
import cv2

import rospy
import rosparam
import sensor_msgs.msg
import geometry_msgs.msg
import message_filters

from psdf import PSDF
from configs import config, DEVICE

def compute_surface_normal(point_map):
    height, width, _ = point_map.shape
    s = 1

    # lower - upper
    coor_up = torch.zeros_like(point_map).to(point_map.device)
    coor_down = torch.zeros_like(point_map).to(point_map.device)
    coor_up[s:height, ...] = point_map[0:height - s, ...]
    coor_down[0:height - s, ...] = point_map[s:height, ...]
    dx = coor_down - coor_up

    # right - left
    coor_left = torch.zeros_like(point_map).to(point_map.device)
    coor_right = torch.zeros_like(point_map).to(point_map.device)
    coor_left[:, s:width, :] = point_map[:, 0:width - s, ...]
    coor_right[:, 0:width - s, :] = point_map[:, s:width, ...]
    dy = coor_right - coor_left

    # normal
    surface_normal = torch.cross(dx, dy, dim=-1)
    norm_normal = torch.norm(surface_normal, dim=-1)
    norm_mask = (norm_normal == 0)
    surface_normal[norm_mask] = 0
    surface_normal[~norm_mask] = surface_normal[~norm_mask] / norm_normal[~norm_mask][:, None]
    return surface_normal

def flatten(psdf, smooth=False, ksize=5, sigmaColor=0.1, sigmaSpace=5):

    # find surface point
    surface_mask = psdf.sdf <= 0.01
    surface_mask_flat = torch.max(surface_mask, dim=-1)[0]

    # get height map
    z_vol = torch.zeros_like(psdf.sdf).long()
    z_vol[surface_mask] = psdf.indices[surface_mask][:, 2]
    z_flat = torch.max(z_vol, dim=-1)[0]
    height_map = psdf.positions[..., 2].take(z_flat)
    if smooth:
        height_map = cv2.bilateralFilter(height_map, ksize, sigmaColor, sigmaSpace)

    # get point map
    point_map = psdf.positions[:, :, 0, :].clone()
    point_map[..., 2] = height_map

    # get normal map
    normal_map = compute_surface_normal(point_map)

    # get variance map
    variances_map = psdf.var.take(z_flat)
    variances_map[~surface_mask_flat] = 10

    # get color map
    color_map = psdf.rgb.take(z_flat)

    # re-arrangement
    # variances_map = torch.flip(variances_map, dims=[0,1])
    # point_map = torch.flip(point_map, dims=[0,1])
    # # normals = torch.flip(normals, dims=[0,1])
    # color_map = torch.flip(color_map, dims=[0,1])

    return (point_map.cpu().numpy(), 
            normal_map.cpu().numpy(), 
            variances_map.cpu().numpy(), 
            color_map.cpu().numpy())

def get_point_cloud(psdf):
    verts, _, _, _ = measure.marching_cubes(psdf.sdf.cpu().numpy(), 0)
    return (verts * config.volume_resolution) @ config.T_volume_to_world[:3, :3] + config.T_volume_to_world[:3, 3]
    # return psdf.positions[psdf.sdf <= 0.01].cpu().numpy()

def main():
    rospy.init_node("psdf")
    show = rosparam.get_param(rospy.get_name() + "/show")

    # initialize PSDF
    psdf = PSDF(config.volume_shape, config.volume_resolution, device=DEVICE, with_color=True)

    # load camera intrinsic and hand-eye-calibration
    with open(os.path.join(os.path.dirname(__file__), "../config/cam_info_realsense.json"), 'r') as f:
        cam_info = json.load(f)
    cam_intr = np.array(cam_info["K"]).reshape(3, 3)
    cam_height = cam_info["height"]
    cam_width = cam_info["width"]
    T_cam_to_tool0 = np.array(cam_info["cam_to_tool0"]).reshape(4, 4)

    # publish "point map" which is required by analysis module
    point_map_pub = rospy.Publisher(rospy.get_name() + "/point_map", sensor_msgs.msg.Image, queue_size=1)
    normal_map_pub = rospy.Publisher(rospy.get_name() + "/normal_map", sensor_msgs.msg.Image, queue_size=1)
    variance_map_pub = rospy.Publisher(rospy.get_name() + "/variance_map", sensor_msgs.msg.Image, queue_size=1)
    if show:
        point_cloud_pub = rospy.Publisher(rospy.get_name() + "/point_cloud", sensor_msgs.msg.PointCloud, queue_size=1)
        point_image_pub = rospy.Publisher(rospy.get_name() + "/point_image", sensor_msgs.msg.Image, queue_size=1)
        normal_image_pub = rospy.Publisher(rospy.get_name() + "/normal_image", sensor_msgs.msg.Image, queue_size=1)
        variance_image_pub = rospy.Publisher(rospy.get_name() + "/variance_image", sensor_msgs.msg.Image, queue_size=1)
        depth_cloud_pub = rospy.Publisher(rospy.get_name() + "/depth_cloud", sensor_msgs.msg.PointCloud, queue_size=1)
        rospy.loginfo("PSDF show topic:")
        rospy.loginfo("\t" + point_cloud_pub.resolved_name)
        rospy.loginfo("\t" + point_image_pub.resolved_name)
        rospy.loginfo("\t" + normal_image_pub.resolved_name)
        rospy.loginfo("\t" + variance_image_pub.resolved_name)
        rospy.loginfo("\t" + depth_cloud_pub.resolved_name)

    # subscribe camera and robot pose
    # and fuse new data to PSDF
    def fuse_cb(depth_msg : sensor_msgs.msg.Image, 
                color_msg : sensor_msgs.msg.Image, 
                tool0_pose_msg : geometry_msgs.msg.Pose):
        assert(depth_msg.height == cam_height and depth_msg.width == cam_width)
        assert(color_msg.height == cam_height and color_msg.width == cam_width)
        depth = np.frombuffer(depth_msg.data, dtype=np.uint16).astype(np.float32).reshape(cam_height, cam_width) / 1000
        color = np.frombuffer(color_msg.data, dtype=np.uint8).reshape(cam_height, cam_width, 3)
        
        p, q = tool0_pose_msg.position, tool0_pose_msg.orientation
        T_tool0_to_world = np.eye(4)
        T_tool0_to_world[:3, :3] = R.from_quat([q.x, q.y, q.z, q.w]).as_matrix()
        T_tool0_to_world[:3, 3] = [p.x, p.y, p.z]
        T_cam_to_world = T_tool0_to_world @ T_cam_to_tool0
        T_cam_to_volume = np.linalg.inv(config.T_volume_to_world) @ T_cam_to_world
        
        # fuse new data
        psdf.fuse(np.copy(depth), cam_intr, T_cam_to_volume, color=np.copy(color))
        # flatten to 2D point image
        point_map, normal_map, variance_map, _ = flatten(psdf)

        # publishing topic message
        point_map_pub.publish(sensor_msgs.msg.Image(
            data=point_map.tobytes(), height=psdf.shape[0], width=psdf.shape[1]
        ))
        normal_map_pub.publish(sensor_msgs.msg.Image(
            data=normal_map.tobytes(), height=psdf.shape[0], width=psdf.shape[1]
        ))
        variance_map_pub.publish(sensor_msgs.msg.Image(
            data=variance_map.tobytes(), height=psdf.shape[0], width=psdf.shape[1]
        ))
        if show:
            # point cloud
            point_cloud_msg = sensor_msgs.msg.PointCloud()
            point_cloud_msg.header.frame_id = "base_link"
            point_cloud = get_point_cloud(psdf)
            point_cloud_msg.points = []
            for point in point_cloud:
                point_cloud_msg.points.append(geometry_msgs.msg.Point32(x=point[0], y=point[1], z=point[2]))
            point_cloud_pub.publish(point_cloud_msg)
            # point image
            point_image_pub.publish(sensor_msgs.msg.Image(
                data=((point_map[..., 2] - config.T_volume_to_world[2, 3]) / config.volume_range[2]).tobytes(),
                height=psdf.shape[0], width=psdf.shape[1],
                encoding="32FC1"
            ))
            # normal image
            normal_image_pub.publish(sensor_msgs.msg.Image(
                data=((normal_map[..., 2] + 1) / 2).tobytes(),
                height=psdf.shape[0], width=psdf.shape[1],
                encoding="32FC1"
            ))
            # variance image
            variance_image_pub.publish(sensor_msgs.msg.Image(
                data=((variance_map - variance_map.min()) / (variance_map.max() - variance_map.min())).tobytes(),
                height=psdf.shape[0], width=psdf.shape[1],
                encoding="32FC1"
            ))
            # depth cloud
            depth_cloud_msg = sensor_msgs.msg.PointCloud()
            depth_cloud_msg.header.frame_id = "base_link"
            I, J = np.meshgrid(np.arange(depth.shape[0]), np.arange(depth.shape[1]), indexing="ij")
            point_cloud = np.stack([J+0.5, I+0.5, np.ones_like(I)], axis=-1) @ np.linalg.inv(cam_intr).T * depth[..., None]
            point_cloud = point_cloud.reshape(-1, 3)
            point_cloud = point_cloud @ T_cam_to_world[:3, :3] + T_cam_to_world[:3, 3]
            depth_cloud_msg.points = []
            for point in point_cloud:
                depth_cloud_msg.points.append(geometry_msgs.msg.Point32(x=point[0], y=point[1], z=point[2]))
            depth_cloud_pub.publish(depth_cloud_msg)

    depth_sub = message_filters.Subscriber(cam_info["depth_topic"], sensor_msgs.msg.Image)
    color_sub = message_filters.Subscriber(cam_info["color_topic"], sensor_msgs.msg.Image)
    tool0_sub = message_filters.Subscriber(cam_info["tool0_pose_topic"], geometry_msgs.msg.Pose)
    sub_syn = message_filters.ApproximateTimeSynchronizer([depth_sub, color_sub, tool0_sub], 1e10, 1e-3, allow_headerless=True)
    sub_syn.registerCallback(fuse_cb)

    rospy.loginfo("PSDF running")
    rospy.spin()


if __name__=="__main__":
    main()