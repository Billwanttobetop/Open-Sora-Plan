import random
import argparse
import cv2
from tqdm import tqdm
import numpy as np
import numpy.typing as npt
import torch
from decord import VideoReader, cpu
from torch.nn import functional as F
from pytorchvideo.transforms import ShortSideScale
from torchvision.transforms import Lambda, Compose
from torchvision.transforms._transforms_video import CenterCropVideo
import sys
from torch.utils.data import Dataset, DataLoader, Subset
import os

sys.path.append(".")
from opensora.models.ae.videobase import CausalVAEModel
from opensora.models.ae.videobase.modules.conv import BlockwiseConv3d
import torch.nn as nn


def replace_conv3d_with_blockwise(model):
    for name, module in model.named_children():
        if isinstance(module, nn.Conv3d):
            new_module = BlockwiseConv3d(
                module.in_channels,
                module.out_channels,
                module.kernel_size,
                module.stride,
                module.padding,
                (4, 64, 64),
            )
            new_module.set_weights(
                module.weight.data,
                module.bias.data if module.bias is not None else None,
            )
            setattr(model, name, new_module)
        else:
            replace_conv3d_with_blockwise(module)


def process_in_chunks(
    video_data: torch.Tensor,
    model: nn.Module,
    chunk_size: int,
    overlap: int,
    device: str,
):
    assert (chunk_size + overlap - 1) % 4 == 0
    num_frames = video_data.size(2)
    output_chunks = []

    start = 0
    while start < num_frames:
        end = min(start + chunk_size, num_frames)
        if start + chunk_size + overlap < num_frames:
            end += overlap
        chunk = video_data[:, :, start:end, :, :]

        with torch.no_grad():
            chunk = chunk.half().to(device)
            latents = model.encode(chunk)
            recon_chunk = model.decode(latents.sample().half()).cpu().float()

        if output_chunks:
            overlap_step = min(overlap, recon_chunk.shape[2])
            overlap_tensor = (
                output_chunks[-1][:, :, -overlap_step:] * 1 / 4
                + recon_chunk[:, :, :overlap_step] * 3 / 4
            )
            output_chunks[-1] = torch.cat(
                (output_chunks[-1][:, :, :-overlap], overlap_tensor), dim=2
            )
            if end < num_frames:
                output_chunks.append(recon_chunk[:, :, overlap:])
            else:
                output_chunks.append(recon_chunk[:, :, :, :, :])
        else:
            output_chunks.append(recon_chunk)
        start += chunk_size
    return torch.cat(output_chunks, dim=2)


def array_to_video(
    image_array: npt.NDArray, fps: float = 30.0, output_file: str = "output_video.mp4"
) -> None:
    height, width, channels = image_array[0].shape
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    video_writer = cv2.VideoWriter(output_file, fourcc, float(fps), (width, height))

    for image in image_array:
        image_rgb = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        video_writer.write(image_rgb)

    video_writer.release()


def custom_to_video(
    x: torch.Tensor, fps: float = 2.0, output_file: str = "output_video.mp4"
) -> None:
    x = x.detach().cpu()
    x = torch.clamp(x, -1, 1)
    x = (x + 1) / 2
    x = x.permute(1, 2, 3, 0).numpy()
    x = (255 * x).astype(np.uint8)
    array_to_video(x, fps=fps, output_file=output_file)
    return


def read_video(video_path: str, num_frames: int, sample_rate: int) -> torch.Tensor:
    decord_vr = VideoReader(video_path, ctx=cpu(0), num_threads=8)
    total_frames = len(decord_vr)
    sample_frames_len = sample_rate * num_frames

    if total_frames > sample_frames_len:
        s = 0
        e = s + sample_frames_len
        num_frames = num_frames
    else:
        s = 0
        e = total_frames
        num_frames = int(total_frames / sample_frames_len * num_frames)
        print(
            f"sample_frames_len {sample_frames_len}, only can sample {num_frames * sample_rate}",
            video_path,
            total_frames,
        )

    frame_id_list = np.linspace(s, e - 1, num_frames, dtype=int)
    video_data = decord_vr.get_batch(frame_id_list).asnumpy()
    video_data = torch.from_numpy(video_data)
    video_data = video_data.permute(3, 0, 1, 2)  # (T, H, W, C) -> (C, T, H, W)
    return video_data


class RealVideoDataset(Dataset):
    def __init__(
        self,
        real_video_dir,
        num_frames,
        sample_rate=1,
        crop_size=None,
        resolution=128,
    ) -> None:
        super().__init__()
        self.real_video_files = self._combine_without_prefix(real_video_dir)
        self.num_frames = num_frames
        self.sample_rate = sample_rate
        self.crop_size = crop_size
        self.short_size = resolution

    def __len__(self):
        return len(self.real_video_files)

    def __getitem__(self, index):
        if index >= len(self):
            raise IndexError
        real_video_file = self.real_video_files[index]
        real_video_tensor = self._load_video(real_video_file)
        video_name = os.path.basename(real_video_file)
        return {'video': real_video_tensor, 'file_name': video_name }

    def _load_video(self, video_path):
        num_frames = self.num_frames
        sample_rate = self.sample_rate
        decord_vr = VideoReader(video_path, ctx=cpu(0))
        total_frames = len(decord_vr)
        sample_frames_len = sample_rate * num_frames

        if total_frames > sample_frames_len:
            s = 0
            e = s + sample_frames_len
            num_frames = num_frames
        else:
            s = 0
            e = total_frames
            num_frames = int(total_frames / sample_frames_len * num_frames)
            print(
                f"sample_frames_len {sample_frames_len}, only can sample {num_frames * sample_rate}",
                video_path,
                total_frames,
            )

        frame_id_list = np.linspace(s, e - 1, num_frames, dtype=int)
        video_data = decord_vr.get_batch(frame_id_list).asnumpy()
        video_data = torch.from_numpy(video_data)
        video_data = video_data.permute(3, 0, 1, 2)
        return _preprocess(
            video_data, short_size=self.short_size, crop_size=self.crop_size
        )

    def _combine_without_prefix(self, folder_path, prefix="."):
        folder = []
        for name in os.listdir(folder_path):
            if name[0] == prefix:
                continue
            folder.append(os.path.join(folder_path, name))
        folder.sort()
        return folder


def _preprocess(video_data, short_size=128, crop_size=None):
    transform = Compose(
        [
            Lambda(lambda x: ((x / 255.0)*2 - 1)),
            ShortSideScale(size=short_size),
            (
                CenterCropVideo(crop_size=crop_size)
                if crop_size is not None
                else Lambda(lambda x: x)
            ),
        ]
    )
    video_outputs = transform(video_data)
    video_outputs = _format_video_shape(video_outputs)
    return video_outputs


def _format_video_shape(video, time_compress=4, spatial_compress=8):
    time = video.shape[1]
    height = video.shape[2]
    width = video.shape[3]
    new_time = (
        (time - 1 - (time - 1) % time_compress)
        if (time - 1) % time_compress != 0
        else time
    )
    new_height = (
        (height - (height) % spatial_compress)
        if height % spatial_compress != 0
        else height
    )
    new_width = (
        (width - (width) % spatial_compress) if width % spatial_compress != 0 else width
    )
    return video[:, :new_time, :new_height, :new_width]


@torch.no_grad()
def main(args: argparse.Namespace):
    real_video_dir = args.real_video_dir
    generated_video_dir = args.generated_video_dir
    ckpt = args.ckpt
    sample_rate = args.sample_rate
    resolution = args.resolution
    crop_size = args.crop_size
    num_frames = args.num_frames
    sample_rate = args.sample_rate
    device = args.device
    sample_fps = args.sample_fps
    batch_size = args.batch_size
    num_workers = args.num_workers
    subset_size = args.subset_size
    
    if not os.path.exists(args.generated_video_dir):
        os.makedirs(args.generated_video_dir, exist_ok=True)
    
    # ---- Load Model ----
    device = args.device
    vqvae = CausalVAEModel.load_from_checkpoint(ckpt)
    vqvae = vqvae.half().to(device)
    # ---- Load Model ----

    # ---- Prepare Dataset ----
    dataset = RealVideoDataset(
        real_video_dir=real_video_dir,
        num_frames=num_frames,
        sample_rate=sample_rate,
        crop_size=crop_size,
        resolution=resolution,
    )
    
    if subset_size:
        indices = range(subset_size)
        dataset = Subset(dataset, indices=indices)
        
    dataloader = DataLoader(
        dataset, batch_size=batch_size, pin_memory=True, num_workers=num_workers
    )
    # ---- Prepare Dataset

    # ---- Inference ----
    for batch in tqdm(dataloader):
        x, file_names = batch['video'], batch['file_name']
        x = x.half().to(device)
        latents = vqvae.encode(x)
        video_recon = vqvae.decode(latents.sample().half())
        for idx, video in enumerate(video_recon):
            output_path = os.path.join(generated_video_dir, file_names[idx])
            custom_to_video(
                video, fps=sample_fps / sample_rate, output_file=output_path
            )
    # ---- Inference ----

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real_video_dir", type=str, default="")
    parser.add_argument("--generated_video_dir", type=str, default="")
    parser.add_argument("--ckpt", type=str, default="")
    parser.add_argument("--sample_fps", type=int, default=30)
    parser.add_argument("--resolution", type=int, default=336)
    parser.add_argument("--crop_size", type=int, default=None)
    parser.add_argument("--num_frames", type=int, default=17)
    parser.add_argument("--sample_rate", type=int, default=1)
    parser.add_argument("--batch_size", type=int, default=1)
    parser.add_argument("--num_workers", type=int, default=8)
    parser.add_argument("--subset_size", type=int, default=None)
    parser.add_argument("--device", type=str, default="cuda")

    args = parser.parse_args()
    main(args)
    
