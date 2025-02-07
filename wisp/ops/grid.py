# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION & AFFILIATES and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION & AFFILIATES is strictly prohibited.

import torch
from kaolin import _C
import wisp._C as wisp_C
import kaolin.ops.spc as spc_ops

# Alternative set of primes
#PRIMES = [2654436881, 5915587277, 1500450271, 3267000013, 5754853343,
#          4093082899, 9576890767, 3628273133, 2860486313, 5463458053,
#          3367900313, 5654500741, 5654500763, 5654500771, 5654500783,
#          5654500801, 5654500811, 5654500861, 5654500879, 5654500889,
#          5654500897, 5654500927, 5654500961, 5654500981, 5654500993,
#          9999999967, 7654487179, 7654489553, 7654495087, 7654486423,
#          7654488209, 8654487029, 8654489771, 8654494517, 8654495341]

PRIMES = [1, 2654435761, 805459861]

def hashgrid_naive(coords, resolutions, codebook_bitwidth, lod_idx, codebook, codebook_lod_sizes, codebook_lod_first_idx):
    """
    A naive PyTorch implementation of the hashgrid.
    This code exists here mostly as a reference:
    Do NOT expect a 1-to-1 numerical correspondence to the CUDA accelerated version.
    This code is comparatively very slow. :)

    Args:
        coords (torch.FloatTensor): 3D coordinates of shape [batch, 3]
        resolutions (torch.LongTensor): the resolution of the grid per level of shape [num_lods]
        codebook_bitwidth (int): The bitwidth of the codebook. The codebook will have 2^bw entries.
        lod_idx (int): The LOD to aggregate to.
        codebook (torch.FloatTensor): A tensor containing the stacked codebooks, each of shape [codebook_size_lod_idx, feature_dim].
        codebook_lod_sizes (torch.IntTensor): A tensor containig the codebook size at each level of detail.
        codebook_lod_first_idx (torch.IntTensor): A tensor containing the first index of each codebook in the stacked codebook tensor.

    Returns:
        (torch.FloatTensor): Features of shape [batch*num_samples, feature_dim]
    """
    codebook_size = 2**codebook_bitwidth

    feats = []
    for i, res in enumerate(resolutions[:lod_idx+1]):
        # This assumes that the input coordinates are in the range [0, 1].
        tf_coords = torch.clip(((coords + 1.0) / 2.0) * res, 0, res-1-1e-5).reshape(-1, 3)
        cc000 = torch.floor(tf_coords).short()
        cc = spc_ops.points_to_corners(cc000).long()

        num_pts = res**3
        if num_pts > codebook_size:
            cidx = (
                    (cc[...,0] * PRIMES[0]) ^ (cc[...,1] * PRIMES[1]) ^ (cc[...,2] * PRIMES[2])
                ) % codebook_size
        else:
            cidx = cc[...,0] + cc[...,1] * res + cc[...,2] * res * res
        # cidx: B, 8

        fs = codebook[codebook_lod_first_idx[i] : codebook_lod_first_idx[i] + codebook_lod_sizes[i]][cidx.reshape(-1)]  # B*8, F
        fs = fs.reshape(-1, 8, fs.shape[-1])  # B, 8, F

        coeffs = torch.zeros(coords.size(0), 8, device=coords.device, dtype=coords.dtype)  # B, 8
        x = tf_coords - cc000
        _x = 1.0 - x

        # Trilinear interpolation
        coeffs[...,0] = _x[...,0] * _x[...,1] * _x[...,2]
        coeffs[...,1] = _x[...,0] * _x[...,1] * x[...,2]
        coeffs[...,2] = _x[...,0] * x[...,1] * _x[...,2]
        coeffs[...,3] = _x[...,0] * x[...,1] * x[...,2]
        coeffs[...,4] = x[...,0] * _x[...,1] * _x[...,2]
        coeffs[...,5] = x[...,0] * _x[...,1] * x[...,2]
        coeffs[...,6] = x[...,0] * x[...,1] * _x[...,2]
        coeffs[...,7] = x[...,0] * x[...,1] * x[...,2]
        coeffs = coeffs.reshape(-1, 8, 1)  # B, 8, 1

        fs_coeffs = (fs * coeffs).sum(1)  # B, F
        feats.append(fs_coeffs)

    # TODO(ttakikawa): This probably does not return according to the num_samples interface
    return torch.cat(feats, -1)  # B, F*L

class HashGridInterpolate(torch.autograd.Function):
    # TODO(ttakikawa): This class should also support the 2D case... which also means I have to write another kernel!

    @staticmethod
    @torch.cuda.amp.custom_fwd(cast_inputs=torch.half)
    def forward(ctx, coords, resolutions, codebook_bitwidth, lod_idx, codebook, codebook_sizes, codebook_first_idx):
        if codebook[0].shape[-1] % 2 == 1:
            raise Exception("The codebook feature dimension needs to be a multiple of 2.")


        # TODO(ttakikawa): Make the kernel use the LOD
        feats_out = wisp_C.ops.hashgrid_interpolate_cuda(coords.float().contiguous(), 
                                                         codebook,
                                                         codebook_first_idx,
                                                         resolutions,
                                                         codebook_bitwidth).contiguous()
    
        ctx.save_for_backward(coords, codebook, codebook_first_idx)
        ctx.resolutions = resolutions
        ctx.num_lods = len(resolutions)
        ctx.codebook_size = 2**codebook_bitwidth
        ctx.codebook_bitwidth = codebook_bitwidth
        ctx.feature_dim = codebook.shape[-1]
        return feats_out

    @staticmethod
    @torch.cuda.amp.custom_bwd
    def backward(ctx, grad_output):

        coords = ctx.saved_tensors[0]
        codebook = ctx.saved_tensors[1]
        codebook_first_idx = ctx.saved_tensors[2]
        resolutions = ctx.resolutions
        feature_dim = ctx.feature_dim
        codebook_bitwidth = ctx.codebook_bitwidth

        grad_codebook = wisp_C.ops.hashgrid_interpolate_backward_cuda(
                coords.float().contiguous(), grad_output.contiguous(), codebook,
                codebook_first_idx,
                resolutions,  
                codebook_bitwidth, feature_dim, ctx.needs_input_grad[0])
        return (None, None, None, None, grad_codebook, None, None)

def hashgrid(coords, resolutions, codebook_bitwidth, lod_idx, codebook, codebook_sizes, codebook_first_idx):
    """A hash-grid query + interpolation function, accelerated with CUDA.

    Args:
        coords (torch.FloatTensor): 3D coordinates of shape [batch, 3]
        resolutions (torch.LongTensor): the resolution of the grid per level of shape [num_lods]
        codebook_bitwidth (int): The bitwidth of the codebook. The codebook will have 2^bw entries.
        lod_idx (int): The LOD to aggregate to.
        codebook (torch.ModuleList[torch.FloatTensor]): A list of codebooks of shapes [codebook_size, feature_dim].

    Returns:
        (torch.FloatTensor): Features of shape [batch, feature_dim]
    """
    batch, dim = coords.shape
    feats = HashGridInterpolate.apply(coords.contiguous(), resolutions,
                                      codebook_bitwidth, lod_idx, codebook,
                                      codebook_sizes, codebook_first_idx)
    feature_dim = codebook.shape[1] * len(resolutions)
    return feats.reshape(batch, feature_dim)
