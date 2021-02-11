import contextlib
import math
from multical.transform.interpolate import interpolate_poses, lerp
import numpy as np

from multical import tables
from multical.transform import matrix, rtvec
# from multical.display import display_pose_projections
from multical.io.logging import LogWriter, info

from . import parameters

from structs.numpy import Table, shape
from structs.struct import concat_lists, apply_none, struct, choose, subset, when

from scipy import optimize

from cached_property import cached_property

default_optimize = struct(
  intrinsics = False,
  board = False,
  rolling = False
)

def select_threshold(quantile=0.95, factor=1.0):
  def f(reprojection_error):
    return np.quantile(reprojection_error, quantile) * factor
  return f




class Calibration(parameters.Parameters):
  def __init__(self, cameras, boards, point_table, camera_poses, board_poses, 
    motion, inlier_mask=None, optimize=default_optimize):

    self.cameras = cameras
    self.boards = boards

    self.point_table = point_table
    self.camera_poses = camera_poses
    self.board_poses = board_poses
    self.motion = motion

    self.optimize = optimize    
    self.inlier_mask = inlier_mask
    
    assert len(self.cameras) == self.size.cameras
    assert camera_poses._shape[0] == self.size.cameras
    assert board_poses._shape[0] == self.size.boards


  @cached_property 
  def size(self):
    cameras, rig_poses, boards, points = self.point_table._prefix
    return struct(cameras=cameras, rig_poses=rig_poses, boards=boards, points=points)

  @cached_property
  def valid(self):

    valid = (np.expand_dims(self.camera_poses.valid, [1, 2]) & 
      np.expand_dims(self.motion.valid_frames, [0, 2]) &
      np.expand_dims(self.board_poses.valid, [0, 1]))

    return self.point_table.valid & np.expand_dims(valid, valid.ndim)


  @cached_property
  def inliers(self):
    return choose(self.inlier_mask, self.valid)


  @cached_property
  def board_points(self):
    return tables.stack_boards(self.boards)

  @cached_property
  def projected(self):
    """ Projected points to each image. 
    Returns a table of points corresponding to point_table"""

    return self.motion.project(self.cameras, self.camera_poses, 
      self.board_poses, self.board_points)

  @cached_property
  def reprojected(self):
    """ Uses the measured points to compute projection motion (if any), 
    to estimate rolling shutter. Only valid for detected points.
    """
    return self.motion.project(self.cameras, self.camera_poses, 
      self.board_poses, self.board_points, self.point_table)


  @cached_property
  def reprojection_error(self):
    return tables.valid_reprojection_error(self.reprojected, self.point_table)

  @cached_property
  def reprojection_inliers(self):
    inlier_table = self.point_table._extend(valid=choose(self.inliers, self.valid))
    return tables.valid_reprojection_error(self.reprojected, inlier_table)



  @cached_property
  def params(self):
    """ Extract parameters as a structs and lists (to be flattened to a vector later)
    """
    def get_pose_params(poses):
        return rtvec.from_matrix(poses.poses).ravel()

    return struct(
      camera_pose = get_pose_params(self.camera_poses),
      board_pose = get_pose_params(self.board_poses),

      motion = self.motion.param_vec,

      camera  = [camera.param_vec for camera in self.cameras
        ] if self.optimize.intrinsics else [], 
      board   = [board.param_vec for board in self.boards
        ] if self.optimize.board else []
    )    
  
  def with_params(self, params):
    """ Return a new Calibration object with updated parameters unpacked from given parameter struct
    sets pose_estimates of boards and cameras, 
    sets camera intrinsic parameters (if optimized),
    sets adjusted board points (if optimized)
    """
    def update_pose(pose_estimates, pose_params):
      m = rtvec.to_matrix(pose_params.reshape(-1, 6))
      return pose_estimates._update(poses=m)

    camera_poses = update_pose(self.camera_poses, params.camera_pose)
    board_poses = update_pose(self.board_poses, params.board_pose)

    motion = self.motion.with_param_vec(params.motion)

    cameras = self.cameras
    if self.optimize.intrinsics:
      cameras = [camera.with_param_vec(p) for p, camera in 
        zip(params.camera, self.cameras)]

    boards = self.boards
    if self.optimize.board:
      boards = [board.with_param_vec(board_params) 
        for board, board_params in zip(boards, params.board)]


    return self.copy(cameras=cameras, camera_poses=camera_poses, board_poses=board_poses,
      boards=boards, motion=motion)

  @cached_property
  def sparsity_matrix(self):
    """ Sparsity matrix for scipy least_squares,
    Mapping between input parameters and output (point) errors.
    Optional - but optimization runs much faster.
    """
    mapper = parameters.IndexMapper(self.inliers)

    param_mappings = (
      mapper.pose_mapping(self.camera_poses, axis=0),
      mapper.pose_mapping(self.board_poses, axis=2),
      self.motion.sparsity(mapper),

      mapper.param_indexes(0, self.params.camera),
      concat_lists([mapper.param_indexes(3, board.reshape(-1, 3)) 
        for board in self.params.board])
    )


    return parameters.build_sparse(sum(param_mappings, []), mapper)

  
  def bundle_adjust(self, tolerance=1e-4, f_scale=1.0, max_iterations=100, loss='linear'):
    """ Perform non linear least squares optimization with scipy least_squares
    based on finite differences of the parameters, on point reprojection error
    """

    def evaluate(param_vec):
      calib = self.with_param_vec(param_vec)
      return (calib.reprojected.points - calib.point_table.points)[self.inliers].ravel()

    with contextlib.redirect_stdout(LogWriter.info()):
      res = optimize.least_squares(evaluate, self.param_vec, jac_sparsity=self.sparsity_matrix, 
        verbose=2, x_scale='jac', f_scale=f_scale, ftol=tolerance, max_nfev=max_iterations, method='trf', loss=loss)
  
    return self.with_param_vec(res.x)
  
  def enable(self, **flags):
    for k in flags.keys():
      assert k in self.optimize,\
        f"unknown option {k}, options are {list(self.optimize.keys())}"

    optimize = self.optimize._extend(**flags)
    return self.copy(optimize=optimize)

  def __getstate__(self):
    attrs = ['cameras', 'boards', 'point_table', 'camera_poses', 'board_poses', 
      'motion', 'inlier_mask', 'optimize'
    ]
    return subset(self.__dict__, attrs)

  def copy(self, **k):
    """Copy calibration environment and change some parameters (no mutation)"""
    d = self.__getstate__()
    d.update(k)
    return Calibration(**d)

  def reject_outliers_quantile(self, quantile=0.95, factor=1.0):
    """ Set inliers based on quantile  """
    threshold = np.quantile(self.reprojection_error, quantile)
    return self.reject_outliers(threshold=threshold * factor)

  
  def reject_outliers(self, threshold):
    """ Set outlier threshold """

    errors, valid = tables.reprojection_error(self.reprojected, self.point_table)
    inliers = (errors < threshold) & valid
    
    num_outliers = valid.sum() - inliers.sum()
    inlier_percent = 100.0 * inliers.sum() / valid.sum()

    info(f"Rejecting {num_outliers} outliers with error > {threshold:.2f} pixels, "
         f"keeping {inliers.sum()} / {valid.sum()} inliers, ({inlier_percent:.2f}%)")

    return self.copy(inlier_mask = inliers)

  def adjust_outliers(self, num_adjustments=4, auto_scale=None, outliers=None, **kwargs):
    info(f"Beginning adjustments ({num_adjustments}) enabled: {self.optimize}, options: {kwargs}")

    for i in range(num_adjustments):
      self.report(f"Adjust_outliers {i}")
      f_scale = apply_none(auto_scale, self.reprojection_error) or 1.0
      if auto_scale is not None:
        info(f"Auto scaling for outliers influence at {f_scale}")
      
      if outliers is not None:
        self = self.reject_outliers(outliers(self.reprojection_error))

      self = self.bundle_adjust(f_scale=f_scale, **kwargs)
    self.report(f"Adjust_outliers end")
    return self


  def plot_errors(self):
    """ Display plots of error distributions"""
    import matplotlib.pyplot as plt

    fig, axs = plt.subplots(2, 1, tight_layout=True)
    errors, valid = tables.reprojection_error(self.reprojected, self.point_table)
    errors, valid = errors.ravel(), valid.ravel()

    inliers = self.inliers.ravel()
    outliers = (valid & ~inliers).ravel()
    
    axs[0].scatter(x = np.arange(errors.size)[inliers], y = errors[inliers], marker=".", label='inlier')  
    axs[0].scatter(x = np.arange(errors.size)[outliers], y = errors[outliers], color='r', marker=".", label='outlier')

    axs[1].hist(errors[valid], bins=50, range=(0, np.quantile(errors[valid], 0.999)))

    plt.show()


  def report(self, stage):
    overall = error_stats(self.reprojection_error)
    inliers = error_stats(self.reprojection_inliers)

    if self.inlier_mask is not None:
      info(f"{stage}: reprojection RMS={inliers.rms:.3f} ({overall.rms:.3f}), "
           f"n={inliers.n} ({overall.n}), quantiles={overall.quantiles}")
    else:
      info(f"{stage}: reprojection RMS={overall.rms:.3f}, n={overall.n}, "
           f"quantiles={overall.quantiles}")



def error_stats(errors):  
  mse = np.square(errors).mean()
  quantiles = np.array([np.quantile(errors, n) for n in [0, 0.25, 0.5, 0.75, 1]])
  return struct(mse = mse, rms = np.sqrt(mse), quantiles=quantiles, n = errors.size)




