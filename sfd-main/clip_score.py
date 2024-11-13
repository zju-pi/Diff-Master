"""Script for calculating CLIP score."""

import csv
import click
import tqdm
import torch
from torch_utils import distributed as dist
from training import dataset
import open_clip
from torchvision import transforms
from torch_utils.download_util import check_file_by_key

#----------------------------------------------------------------------------

@click.group()
def main():
    """Calculate CLIP score.
    python clip-score.py calc --images=path/to/images
    torchrun --standalone --nproc_per_node=1 clip-score.py calc --images=path/to/images
    """

#----------------------------------------------------------------------------

@main.command()
@click.option('--images', 'image_path', help='Path to the images', metavar='PATH|ZIP',              type=str, required=True)
@click.option('--num', 'num_expected',  help='Number of images to use', metavar='INT',              type=click.IntRange(min=2), show_default=True)
@click.option('--seed',                 help='Random seed for selecting the images', metavar='INT', type=int, default=0, show_default=True)
@click.option('--batch',                help='Maximum batch size', metavar='INT',                   type=click.IntRange(min=1), default=250, show_default=True)
@click.option('--desc',                 help='A description string', metavar='str',                 type=str)
@torch.no_grad()
def calc(image_path, batch, desc=None, num_expected=None, seed=0, max_batch_size=64,
    num_workers=3, prefetch_factor=2, device=torch.device('cuda')):
    """Calculate FID for a given set of images."""
    torch.multiprocessing.set_start_method('spawn')
    dist.init()

    # Rank 0 goes first.
    if dist.get_rank() != 0:
        torch.distributed.barrier()

    # List images.
    dist.print0(f'Loading images from "{image_path}"...')
    dataset_obj = dataset.ImageFolderDataset(path=image_path, max_size=num_expected, random_seed=seed)

    # Loading COCO validation set
    prompt_path, _ = check_file_by_key('prompts')
    dist.print0(f"Loading MS-COCO 30k captions...")
    sample_captions = []
    with open(prompt_path, 'r') as file:
        reader = csv.DictReader(file)
        for row in reader:
            text = row['text']
            sample_captions.append(text)

    # Loading CLIP model
    dist.print0(f'Loading CLIP-ViT-g-14 model...')
    model, _, preprocess = open_clip.create_model_and_transforms('ViT-g-14', pretrained='laion2b_s34b_b88k')
    tokenizer = open_clip.get_tokenizer('ViT-g-14')
    model.to(device)

    # Other ranks follow.
    if dist.get_rank() == 0:
        torch.distributed.barrier()

    # Divide images into batches.
    num_batches = ((len(dataset_obj) - 1) // (max_batch_size * dist.get_world_size()) + 1) * dist.get_world_size()
    all_batches = torch.arange(len(dataset_obj)).tensor_split(num_batches)
    rank_batches = all_batches[dist.get_rank() :: dist.get_world_size()]
    data_loader = torch.utils.data.DataLoader(dataset_obj, batch_sampler=rank_batches, num_workers=num_workers, prefetch_factor=prefetch_factor)

    # Accumulate statistics.
    dist.print0(f'Calculating statistics for {len(dataset_obj)} images...')
    avg_clip_score, batch_idx = 0, 0
    to_pil = transforms.ToPILImage()
    for images, _ in tqdm.tqdm(data_loader, unit='batch', disable=(dist.get_rank() != 0)):
        torch.distributed.barrier()
        prompts = sample_captions[rank_batches[batch_idx][0]:rank_batches[batch_idx][-1]+1]

        images = torch.stack([preprocess(to_pil(img)) for img in images], dim=0).to(device)
        text = tokenizer(prompts).to(device)

        image_features = model.encode_image(images)
        text_features = model.encode_text(text)
        image_features /= image_features.norm(dim=-1, keepdim=True)
        text_features /= text_features.norm(dim=-1, keepdim=True)

        sd_clip_score = 100 * (image_features * text_features).sum(axis=-1)
        avg_clip_score += sd_clip_score.sum()
        batch_idx += 1
        # if batch_idx % 10 == 0:
        #     total_samples = batch_idx * max_batch_size
        #     dist.print0(f"CLIP score under {total_samples} samples: {avg_clip_score / total_samples}")
    
    avg_clip_score /= len(dataset_obj)
    dist.print0(f"CLIP score: {avg_clip_score}")

    if dist.get_rank() == 0:
        Note = open('clip_score.txt', mode='a')
        Note.write(f'{desc} {avg_clip_score}\n') if desc is not None else Note.write('{} {} {}\n'.format(image_path.split('/')[-2], image_path.split('/')[-1], avg_clip_score))
        Note.close()

    torch.distributed.barrier()

#----------------------------------------------------------------------------

if __name__ == "__main__":
    main()

#----------------------------------------------------------------------------
