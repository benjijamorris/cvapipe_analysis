import vtk
import math
import operator
import numpy as np
import pandas as pd
from tqdm import tqdm
from pathlib import Path
from functools import reduce
import matplotlib.pyplot as plt
from matplotlib import animation
from aicsshparam import shtools
from distributed import LocalCluster, Client
from typing import Dict, List, Optional, Union
from aics_dask_utils import DistributedHandler
from vtk.util.numpy_support import vtk_to_numpy

from .dim_reduction import pPCA

def filter_extremes_based_on_percentile(
    df: pd.DataFrame,
    features: List,
    pct: float
):

    """
    Exclude extreme data points that fall in the percentile range
    [0,pct] or [100-pct,100] of at least one of the features
    provided.

    Parameters
    --------------------
    df: pandas df
        Input dataframe that contains the features.
    features: List
        List of column names to be used to filter the data
        points.
    pct: float
        Specifies the percentile range; data points that
        fall in the percentile range [0,pct] or [100-pct,100]
        of at least one of the features are removed.

    Returns
    -------
    df: pandas dataframe
        Filtered dataframe.
    """
    
    # Temporary column to store whether a data point is an
    # extreme point or not.
    df["extreme"] = False

    for f in features:

        # Calculated the extreme interval fot the current feature
        finf, fsup = np.percentile(df[f].values, [pct, 100 - pct])

        # Points in either high or low extreme as flagged
        df.loc[(df[f] < finf), "extreme"] = True
        df.loc[(df[f] > fsup), "extreme"] = True

    # Drop extreme points and temporary column
    df = df.loc[df.extreme == False]
    df = df.drop(columns=["extreme"])

    return df


def digitize_shape_mode(
    df: pd.DataFrame,
    feature: List,
    nbins: int,
    filter_based_on: List,
    filter_extremes_pct: float = 1,
    save: Optional[Path] = None,
    return_freqs_per_structs: Optional[bool] = False
):

    """
    Discretize a given feature into nbins number of equally
    spaced bins. The feature is first z-scored and the interval
    from -2std to 2std is divided into nbins bins.

    Parameters
    --------------------
    df: pandas df
        Input dataframe that contains the feature to be
        discretized.
    features: str
        Column name of the feature to be discretized.
    nbins: int
        Number of bins to divide the feature into.
    filter_extremes_pct: float
        See parameter pct in function filter_extremes_based_on_percentile
    filter_based_on: list
        List of all column names that should be used for
        filtering extreme data points.
    save: Path
        Path to a file where we save the number of data points
        that fall in each bin
    return_freqs_per_structs: bool
        ??
    Returns
    -------
        df: pandas dataframe
            Input dataframe with data points filtered according
            to filter_extremes_pct plus a column named "bin"
            that denotes the bin in which a given data point
            fall in.
        bin_indexes: list of tuples
            [(a,b)] where a is the bin number and b is a list
            with the index of all data points that fall into
            that bin.
        bin_centers: list
            List with values of feature at the center of each
            bin
        pc_std: float
            Standard deviation used to z-score the feature.

    """
    
    # Check if feature is available
    if feature not in df.columns:
        raise ValueError(f"Column {feature} not found.")

    # Exclude extremeties
    df = filter_extremes_based_on_percentile(
        df = df,
        features = filter_based_on,
        pct = filter_extremes_pct
    )

    # Get feature values
    values = df[feature].values.astype(np.float32)

    # Should be centered already, but enforce it here
    values -= values.mean()
    # Z-score
    
    pc_std = values.std()
    values /= pc_std

    # Calculate bin half width based on std interval and nbins
    LINF = -2.0 # inferior limit = -2 std
    LSUP = 2.0 # superior limit = 2 std
    binw = (LSUP-LINF)/(2*(nbins-1))
    
    # Force samples below/above -/+ 2std to fall into first/last bin
    bin_centers = np.linspace(LINF, LSUP, nbins)
    bin_edges = np.unique([(b-binw, b+binw) for b in bin_centers])
    bin_edges[0] = -np.inf
    bin_edges[-1] = np.inf
    
    # Aplly digitization
    df["bin"] = np.digitize(values, bin_edges)

    # Report number of data points in each bin
    df_freq = pd.DataFrame(df["bin"].value_counts(sort=False))
    df_freq.index = df_freq.index.rename(f"{feature}_bin")
    df_freq = df_freq.rename(columns={"bin": "samples"})
    if save is not None:
        with open(f"{save}.txt", "w") as flog:
            print(df_freq, file=flog)

    # Store the index of all data points in each bin
    bin_indexes = []
    df_agg = df.groupby(["bin"]).mean()
    for b, df_bin in df.groupby(["bin"]):
        bin_indexes.append((b, df_bin.index))

    # Optionally return a dataframe with the number of data
    # points in each bin stratifyied by structure_name.
    if return_freqs_per_structs:
        df_freq = (
            df[["structure_name", "bin"]].groupby(["structure_name", "bin"]).size()
        )
        df_freq = pd.DataFrame(df_freq)
        df_freq = df_freq.rename(columns={0: "samples"})
        df_freq = df_freq.unstack(level=1)
        return df_agg, bin_indexes, (bin_centers, pc_std), df_freq

    return df, bin_indexes, (bin_centers, pc_std)

def find_plane_mesh_intersection(
    proj: List,
    mesh: vtk.vtkPolyData
):

    """
    Determine the points of mesh that intersect with the
    plane defined by the proj:

    Parameters
    --------------------
    proj: List
        One of [0,1], [0,2] or [1,2] for xy-plane, xz-plane
        and yz-plane, respectively.
    mesh: vtk.vtkPolyData
        Input triangle mesh.
    Returns
    -------
        points: np.array
            Nx3 array of xyz coordinates of mesh points
            that intersect the plane.
    """
    
    # Find axis orthogonal to the projection of interest
    ax = [a for a in [0, 1, 2] if a not in proj][0]

    # Get all mesh points
    points = vtk_to_numpy(mesh.GetPoints().GetData())

    if not np.abs(points[:, ax]).sum():
        raise Exception("Only zeros found in the plane axis.")

    mid = np.mean(points[:, ax])

    # Set the plane a little off center to avoid undefined intersections
    # Without this the code hangs when the mesh has any edge aligned with the
    # projection plane
    mid += 0.75
    offset = 0.1 * np.ptp(points, axis=0).max()

    # Create a vtkPlaneSource
    plane = vtk.vtkPlaneSource()
    plane.SetXResolution(4)
    plane.SetYResolution(4)
    if ax == 0:
        plane.SetOrigin(mid, points[:, 1].min() - offset, points[:, 2].min() - offset)
        plane.SetPoint1(mid, points[:, 1].min() - offset, points[:, 2].max() + offset)
        plane.SetPoint2(mid, points[:, 1].max() + offset, points[:, 2].min() - offset)
    if ax == 1:
        plane.SetOrigin(points[:, 0].min() - offset, mid, points[:, 2].min() - offset)
        plane.SetPoint1(points[:, 0].min() - offset, mid, points[:, 2].max() + offset)
        plane.SetPoint2(points[:, 0].max() + offset, mid, points[:, 2].min() - offset)
    if ax == 2:
        plane.SetOrigin(points[:, 0].min() - offset, points[:, 1].min() - offset, mid)
        plane.SetPoint1(points[:, 0].min() - offset, points[:, 1].max() + offset, mid)
        plane.SetPoint2(points[:, 0].max() + offset, points[:, 1].min() - offset, mid)
    plane.Update()
    plane = plane.GetOutput()

    # Trangulate the plane
    triangulate = vtk.vtkTriangleFilter()
    triangulate.SetInputData(plane)
    triangulate.Update()
    plane = triangulate.GetOutput()

    # Calculate intersection
    intersection = vtk.vtkIntersectionPolyDataFilter()
    intersection.SetInputData(0, mesh)
    intersection.SetInputData(1, plane)
    intersection.Update()
    intersection = intersection.GetOutput()

    # Get coordinates of intersecting points
    points = vtk_to_numpy(intersection.GetPoints().GetData())

    # Sorting points clockwise
    # This has been discussed here:
    # https://stackoverflow.com/questions/51074984/sorting-according-to-clockwise-point-coordinates/51075469
    # but seems not to be very efficient. Better version is proposed here:
    # https://stackoverflow.com/questions/57566806/how-to-arrange-the-huge-list-of-2d-coordinates-in-a-clokwise-direction-in-python
    coords = points[:, proj]
    center = tuple(
        map(
            operator.truediv,
            reduce(lambda x, y: map(operator.add, x, y), coords),
            [len(coords)] * 2,
        )
    )
    coords = sorted(
        coords,
        key=lambda coord: (
            -135
            - math.degrees(math.atan2(*tuple(map(operator.sub, coord, center))[::-1]))
        )
        % 360,
    )

    # Store sorted coordinates
    points[:, proj] = coords

    return points


def get_shcoeff_matrix_from_dataframe(
    row: pd.Series,
    prefix: str,
    lmax: int
):

    """
    Reshape spherical harmonics expansion (SHE) coefficients
    into a coefficients matrix of shape 2 x lmax x lmax, where
    lmax is the degree of the expansion stored in input
    pandas series.

    Parameters
    --------------------
    row: pd.Series
        Series that contains the SHE coefficients.
    prefix: str
        String to identify the keys of the series that contain
        the SHE coefficients.
    lmax: int
        Degree of the expansion
    Returns
    -------
        coeffs: np.array
            Array of shape 2 x lmax x lmax that contains the
            SHE coefficients.
    """
    
    # Empty matrix to store the SHE coefficients
    coeffs = np.zeros((2, lmax, lmax), dtype=np.float32)

    for l in range(lmax):
        for m in range(l + 1):
            try:
                # Cosine SHE coefficients
                coeffs[0, l, m] = row[[f for f in row.keys() if f"{prefix}{l}M{m}C" in f]]
                # Sine SHE coefficients
                coeffs[1, l, m] = row[[f for f in row.keys() if f"{prefix}{l}M{m}S" in f]]
            # If a given (l,m) pair is not found, it is
            # assumed to be zero
            except:
                pass

    # Error if no coefficients were found.
    if not np.abs(coeffs).sum():
        raise Exception(
            f"No coefficients found. Please check prefix: {prefix}"
        )

    return coeffs

def get_mesh_from_dataframe(
    row: pd.Series,
    prefix: str,
    lmax: int
):

    """
    Reconstruct the 3D triangle mesh corresponding to SHE
    coefficients stored in a pandas Series format.

    Parameters
    --------------------
    row: pd.Series
        Series that contains the SHE coefficients.
    prefix: str
        String to identify the keys of the series that contain
        the SHE coefficients.
    lmax: int
        Degree of the expansion
    Returns
    -------
        mesh: vtk.vtkPolyData
            Triangle mesh.
    """
    
    # Reshape SHE coefficients
    coeffs = get_shcoeff_matrix_from_dataframe(
        row = row,
        prefix = prefix,
        lmax = lmax
    )

    # Use aicsshparam to convert SHE coefficients into
    # triangle mesh
    mesh, _ = shtools.get_reconstruction_from_coeffs(coeffs)

    return mesh


def get_contours_of_consecutive_reconstructions(
    df: pd.DataFrame,
    prefix: str,
    proj: List,
    lmax: int
):

    """
    Reconstruct the 3D triangle mesh corresponding to SHE
    coefficients per index of the input dataframe and finds
    the intersection between this mesh and a plane defined
    by the input variable proj. The intersection serves as
    a 2D contour of the mesh.

    Parameters
    --------------------
    df: pd.DataFrame
        dataframe that contains SHE coefficients that will be
        used to reconstruct a triangle mesh per index.
    prefix: str
        String to identify the keys of the series that contain
        the SHE coefficients.
    proj: List
        One of [0,1], [0,2] or [1,2] for xy-plane, xz-plane
        and yz-plane, respectively.
    lmax: int
        Degree of the expansion
    Returns
    -------
        contours: List
            List of xyz points that intersect the reconstrcuted
            meshes and the plane defined by proj. One per index.
        meshes: List
            List of reconstructed meshes. One per index.
        limits: List
            List of limits of reconstructed meshes. One per
            index.
    TBD
    ---
    
        - Set bin as index of the dataframe outside this
        function.
    
    """
    
    if "bin" in df.columns:
        df = df.set_index("bin")

    meshes = []
    limits = []
    contours = []

    for index, row in df.iterrows():

        # Get mesh of current index
        mesh = get_mesh_from_dataframe(
            row = row,
            prefix = prefix,
            lmax = lmax
        )

        # Find intersection between current mesh and plane
        # defined by the input projection.
        proj_points = find_plane_mesh_intersection(proj=proj, mesh=mesh)

        # Find x, y and z limits of mesh points coordinates
        limit = mesh.GetBounds()

        meshes.append(mesh)
        limits.append(limit)
        contours.append(proj_points)

    return contours, meshes, limits

def get_shcoeffs_from_pc_coords(
    coords: np.array,
    pc: int,
    pca: pPCA
):
    
    """
    Uses the inverse PCA transform to convert one or more PC
    coordiantes back into SHE coefficients.
    
    Parameters
    --------------------
    coords: np.array
        One or more values along the principal component
        denoted by pc.
    pc: int
        Integer that denotes the principal components the
        coordinates refer to.
    pca: sklearn.decomposition.PCA
        PCA object to be used.
    Returns
    -------
        df_coeffs: pd.DataFrame
        DataFrame that stores the SHE coefficients.
        
    TBD:
        Class for PCA object that stores the features names.
    """
    
    # coords has shape (N,)
    npts = len(coords)
    # Creates a matrix of shape (N,M), where M is the
    # reduced dimension
    pc_coords = np.zeros((npts, pca.get_pca().n_components), dtype=np.float32)
    # Copy input coordinates to the matrix
    pc_coords[:, pc] = coords
    # Uses inverse PCA and stores result into a dataframe
    df_coeffs = pd.DataFrame(pca.get_pca().inverse_transform(pc_coords))
    df_coeffs.columns = pca.get_feature_names()
    df_coeffs.index = np.arange(1, 1 + npts)

    return df_coeffs

def transform_coords_to_mem_space(
    xo: float,
    yo: float,
    zo: float,
    angle: float,
    cm: List
):

    """
    Converts a xyz-coordinate into coordinate system of
    aligned cell, defined by the angle and cell centroid.
    
    Parameters
    --------------------
    xo: float
        x-coordinate
    yo: float
        y-coordinate
    zo: float
        z-coordinate
    angle: float
        Cell alignment angle in degrees.
    cm: tuple
        xyz-coordinates of cell centroid.
    Returns
    -------
        xt: float
        Transformed x-coodinate
        yt: float
        Transformed y-coodinate
        zt: float
        Transformed z-coodinate
    """
    
    angle = np.pi * angle / 180.0

    rot_mx = np.array(
        [
            [np.cos(angle), np.sin(angle), 0],
            [-np.sin(angle), np.cos(angle), 0],
            [0, 0, 1],
        ]
    )

    pt_rot = np.matmul(rot_mx, np.array([xo-cm[0], yo-cm[1], zo-cm[2]]))

    xt = pt_rot[0]
    yt = pt_rot[1]
    zt = pt_rot[2]

    return xt, yt, zt

def animate_shape_modes_and_save_meshes(
    df: pd.DataFrame,
    df_agg: pd.DataFrame,
    bin_indexes: List,
    feature: str,
    save: Path,
    plot_limits: Optional[bool] = None,
    fix_nuclear_position: Optional[bool] = True,
    distributed_executor_address: Optional[str] = None,
):

    """
    Uses the inverse PCA transform to convert one or more PC
    coordiantes back into SHE coefficients.
    
    Parameters
    --------------------
    df: pd.DataFrame
        
    df_agg: pd.DataFrame
        
    bin_indexes: List
        
    feature: str
        
    save: Path
        
    plot_limits: Optional[bool] = None
        
    fix_nuclear_position: Optional[bool] = True
        
    distributed_executor_address: Optionalstr = None
        
    Returns
    -------
        df_coeffs: pd.DataFrame
        DataFrame that stores the SHE coefficients.
        
    TBD:
        Class for PCA object that stores the features names.
    """
    
    if fix_nuclear_position:

        def process_this_index(index_row):
            '''
            Change the coordinate system of nuclear centroid
            from nuclear to the aligned cell.
            '''
            index, row = index_row

            dxc, dyc, dzc = transform_coords_to_mem_space(
                xo = row["dna_position_x_centroid_lcc"],
                yo = row["dna_position_y_centroid_lcc"],
                zo = row["dna_position_z_centroid_lcc"],
                # Cell alignment angle
                angle = row["mem_shcoeffs_transform_angle_lcc"],
                # Cell centroid
                cm = [row[f"mem_position_{k}_centroid_lcc"] for k in ["x", "y", "z"]],
            )

            return (dxc, dyc, dzc)

        # Change the reference system of the vector that
        # defines the nuclear location relative to the cell
        # of all cells that fall into the same bin.
        for (b, indexes) in bin_indexes:
            # Subset with cells from the same bin.
            df_tmp = df.loc[df.index.isin(indexes)]            
            # Change reference system for all cells in parallel.
            nuclei_cm_fix = []
            with DistributedHandler(distributed_executor_address) as handler:
                future = handler.batched_map(
                    process_this_index,
                    [index_row for index_row in df_tmp.iterrows()],
                )
                nuclei_cm_fix.append(future)
            # Average changed nuclear centroid over all cells
            mean_nuclei_cm_fix = np.array(nuclei_cm_fix[0]).mean(axis=0)
            # Store
            df_agg.loc[b, "dna_dxc"] = mean_nuclei_cm_fix[0]
            df_agg.loc[b, "dna_dyc"] = mean_nuclei_cm_fix[1]
            df_agg.loc[b, "dna_dzc"] = mean_nuclei_cm_fix[2]
            
    else:
        # Save nuclear displacement as zeros if no adjustment
        # is requested.
        for (b, indexes) in bin_indexes:

            df_agg.loc[b, "dna_dxc"] = 0
            df_agg.loc[b, "dna_dyc"] = 0
            df_agg.loc[b, "dna_dzc"] = 0

    hlimits = []
    vlimits = []
    all_mem_contours = []
    all_dna_contours = []

    # Loop over 3 different projections: xy=[0,1], xz=[0,2] and
    # yz=[1,2]
    for proj_id, projection in enumerate([[0, 1], [0, 2], [1, 2]]):

        # Get nuclear meshes and their 2D projections
        # for 3 different projections,xy, xz and yz.
        mem_contours, mem_meshes, mem_limits = get_contours_of_consecutive_reconstructions(
            df = df_agg,
            prefix = "mem_shcoeffs_L",
            proj = projection,
            lmax = 32
        )
        # Get cells meshes and their 2D projections
        # for 3 different projections,xy, xz and yz.
        dna_contours, dna_meshes, dna_limits = get_contours_of_consecutive_reconstructions(
            df = df_agg,
            prefix = "dna_shcoeffs_L",
            proj = projection,
            lmax = 32
        )

        # Change the nuclear position relative to the cell
        # in the reconstructed meshes when running the
        # first projection
        if proj_id == 0:
            for (b, indexes), mem_mesh, dna_mesh in zip(bin_indexes, mem_meshes, dna_meshes):
                for i in range(dna_mesh.GetNumberOfPoints()):
                    # Meshes are centered at origin when
                    # reconstructed.
                    r = dna_mesh.GetPoints().GetPoint(i)
                    u = np.array(r).copy()
                    u[0] += df_agg.loc[b, "dna_dxc"]
                    u[1] += df_agg.loc[b, "dna_dyc"]
                    u[2] += df_agg.loc[b, "dna_dzc"]
                    dna_mesh.GetPoints().SetPoint(i, u)

                shtools.save_polydata(mem_mesh, f"{save}/MEM_{feature}_{b:02d}.vtk")
                shtools.save_polydata(dna_mesh, f"{save}/DNA_{feature}_{b:02d}.vtk")

        all_mem_contours.append(mem_contours)
        all_dna_contours.append(dna_contours)

        xmin = np.min([b[0] for b in mem_limits])
        xmax = np.max([b[1] for b in mem_limits])
        ymin = np.min([b[2] for b in mem_limits])
        ymax = np.max([b[3] for b in mem_limits])
        zmin = np.min([b[4] for b in mem_limits])
        zmax = np.max([b[5] for b in mem_limits])

        hlimits += [xmin, xmax, ymin, ymax]
        vlimits += [ymin, ymax, zmin, zmax]

    hmin = np.min(hlimits)
    hmax = np.max(hlimits)
    vmin = np.min(vlimits)
    vmax = np.max(vlimits)

    if plot_limits is not None:
        hmin, hmax, vmin, vmax = plot_limits

    offset = 0.05 * (hmax - hmin)

    for projection, mem_contours, dna_contours in zip(
        [[0, 1], [0, 2], [1, 2]], all_mem_contours, all_dna_contours
    ):

        # Animate contours

        hcomp = projection[0]
        vcomp = projection[1]

        fig, ax = plt.subplots(1, 1, figsize=(3, 3))
        plt.close()
        ax.set_xlim(hmin - offset, hmax + offset)
        ax.set_ylim(vmin - offset, vmax + offset)
        ax.set_aspect("equal")

        (mline,) = ax.plot(
            [], [], lw=2, color="#F200FF" if "MEM" in feature else "#3AADA7"
        )
        (dline,) = ax.plot(
            [], [], lw=2, color="#3AADA7" if "DNA" in feature else "#F200FF"
        )

        def animate(i):

            mct = mem_contours[i]
            mx = mct[:, hcomp]
            my = mct[:, vcomp]

            dct = dna_contours[i]
            dx = dct[:, hcomp]
            dy = dct[:, vcomp]

            hlabel = ["x", "y", "z"][[0, 1, 2].index(projection[0])]
            vlabel = ["x", "y", "z"][[0, 1, 2].index(projection[1])]
            dx = dx + df_agg.loc[i + 1, f"dna_d{hlabel}c"]
            dy = dy + df_agg.loc[i + 1, f"dna_d{vlabel}c"]

            mline.set_data(mx, my)
            dline.set_data(dx, dy)

            return (
                mline,
                dline,
            )

        anim = animation.FuncAnimation(
            fig, animate, frames=len(mem_contours), interval=100, blit=True
        )

        anim.save(
            f"{save}/{feature}_{''.join(str(x) for x in projection)}.gif",
            writer="imagemagick",
            fps=len(mem_contours),
        )

        plt.close("all")


def reconstruct_shape_mode(
    pca,
    features,
    mode,
    mode_name,
    map_points,
    reconstruct_on,
    save,
    lmax=32,
    plot_limits=None,
):

    # Use inverse PCA to transform PC coordinates back to SH coefficients
    npts = len(map_points)
    pc_coords = np.zeros((npts, pca.n_components), dtype=np.float32)
    pc_coords[:, mode] = map_points
    df_coeffs = pd.DataFrame(pca.inverse_transform(pc_coords))
    df_coeffs.columns = features
    df_coeffs.index = np.arange(1, 1 + npts)

    return df_coeffs

    # Generate figure with outlines
    fig, axs = plt.subplots(
        1, 3 * len(reconstruct_on), figsize=(8 * len(reconstruct_on), 4)
    )

    alphao = 0.3
    alphaf = 0.7
    cmap = plt.cm.get_cmap("jet")

    for row, index in tqdm(
        enumerate(df_coeffs.index),
        total=df_coeffs.shape[0],
        desc=f"{mode}, Bin",
        leave=False,
    ):

        for axo, (prefix, _) in tqdm(
            enumerate(reconstruct_on),
            total=len(reconstruct_on),
            desc="Attribute",
            leave=False,
        ):

            mesh = get_mesh_from_dataframe(
                index=index, df=df_coeffs, prefix=prefix, lmax=lmax
            )

            for axi, proj in tqdm(
                enumerate([[0, 1], [0, 2], [1, 2]]),
                total=3,
                desc="Projection",
                leave=False,
            ):

                proj_points = find_plane_mesh_intersection(proj=proj, mesh=mesh)

                if index == df_coeffs.index[0]:
                    param_c = "blue"
                    param_s = "-"
                    param_w = 2
                    param_a = 1
                elif index == df_coeffs.index[-1]:
                    param_c = "magenta"
                    param_s = "-"
                    param_w = 2
                    param_a = 1
                else:
                    param_c = "gray"
                    param_s = "-"
                    param_w = 1
                    param_a = alphao + (alphaf - alphao) * row / (npts - 1)

                axs[len(reconstruct_on) * axi + axo].plot(
                    proj_points[:, proj[0]],
                    proj_points[:, proj[1]],
                    # c=param_c,
                    c=cmap(row / (npts - 1)),
                    linestyle=param_s,
                    linewidth=param_w,
                    alpha=param_a,
                )

                if plot_limits is None:

                    if "xmin" not in locals():
                        xmin = proj_points[:, proj[0]].min()
                        ymin = proj_points[:, proj[1]].min()
                        xmax = proj_points[:, proj[0]].max()
                        ymax = proj_points[:, proj[1]].max()
                    else:
                        xmin = np.min([proj_points[:, proj[0]].min(), xmin])
                        ymin = np.min([proj_points[:, proj[1]].min(), ymin])
                        xmax = np.max([proj_points[:, proj[0]].max(), xmax])
                        ymax = np.max([proj_points[:, proj[1]].max(), ymax])

                else:

                    xmin, xmax, ymin, ymax = plot_limits

                shtools.save_polydata(mesh, f"{save}_{prefix}_{index:02d}.vtk")

    for axi, labs in enumerate([("X", "Y"), ("X", "Z"), ("Y", "Z")]):
        for axo, rec in zip([0, 1], reconstruct_on):
            axs[len(reconstruct_on) * axi + axo].set_title(rec[1], fontsize=14)
            axs[len(reconstruct_on) * axi + axo].set_xlim(xmin, xmax)
            axs[len(reconstruct_on) * axi + axo].set_ylim(ymin, ymax)
            axs[len(reconstruct_on) * axi + axo].set_aspect("equal")
        plt.figtext(
            0.18 + 0.33 * axi,
            0.85,
            f"{labs[0]}{labs[1]} Projection",
            va="center",
            ha="center",
            size=14,
        )

    fig.suptitle(f"Shape mode: {1+mode} ({mode_name})", fontsize=18)
    fig.subplots_adjust(top=0.78)
    plt.tight_layout()
    plt.savefig(f"{save}.jpg")
    plt.close("all")

    return df_coeffs
