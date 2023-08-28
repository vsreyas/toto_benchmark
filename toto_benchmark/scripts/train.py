"""Train a TOTO agent.

Example command:
python train.py --config-name train_bc.yaml 

Hyperparameters can be set in corresponding .yaml files in confs/
"""

import baselines
from datetime import datetime
import hydra
import logging
import numpy as np
from omegaconf import DictConfig, OmegaConf, open_dict
import os
import pickle
import torch
from torch.utils.data import DataLoader, random_split
import wandb
import matplotlib.pyplot as plt
from matplotlib import animation
from PIL import Image


from dataset_traj import FrankaDatasetTraj
from toto_benchmark.agents import init_agent_from_config
from toto_benchmark.vision import load_transforms, EMBEDDING_DIMS
from toto_benchmark.vision.pvr_model_loading import load_pvr_model, load_pvr_transforms

log = logging.getLogger(__name__)

def global_seeding(seed=0):
    torch.manual_seed(seed)
    np.random.seed(seed)

def save_frames_as_gif(frames, path='./', filename=None, frame_rate_divider=1):
    plt.figure(figsize=(frames[0].shape[1] / 72.0, frames[0].shape[0] / 72.0), dpi=72)

    patch = plt.imshow(frames[0])
    plt.axis('off')

    def animate(i):
        patch.set_data(frames[i])

    anim = animation.FuncAnimation(plt.gcf(), animate, frames = len(frames), interval=50)
    fname = os.path.join(path, filename)
    anim.save(fname, writer='imagemagick', fps=60 / frame_rate_divider)
    print("Saved gif", fname)


def _eval_agent(env, agent, device, model, transforms, epoch, n_rollouts=10):
    episode_rewards = []
    for i in range(n_rollouts):
        obs = env.reset(); done=False; success=False
        t = 0; reward = 0

        frames = []
        frame_rate_divider = 15
        _MAX_STEPS = 1000

        try:
            while not done and t < _MAX_STEPS and not success:
                # In first eval rollout, same frames for gif
                if i == 0 and t % frame_rate_divider == 0:
                    frames.append(obs['image'])

                image = torch.stack((transforms(Image.fromarray(obs['image']).crop((200, 0, 500, 400))),))
                embed = model(image.to(device)).to('cpu').data.numpy()

                o = torch.from_numpy(obs['proprioception'])[None].float()
                obs = np.hstack([o, embed])
                inputs = torch.from_numpy(obs).float()

                action = agent.predict({'inputs': obs})
                obs, reward, done, env_info = env.step(action)
                t += 1
                if done or t >= _MAX_STEPS:
                    break
        except:
            pass

        episode_rewards.append(float(reward))
        if i == 0:
            fname = datetime.now().strftime("%m-%d-%Y-%H-%M-%S_epoch") + str(epoch) + '.gif'
            save_frames_as_gif(frames, path=hydra.utils.get_original_cwd(), filename=fname, frame_rate_divider=frame_rate_divider)
    return np.mean(episode_rewards)


@hydra.main(config_path="../conf", config_name="train_bc")
def main(cfg : DictConfig) -> None:
    with open_dict(cfg):
        cfg['saved_folder'] = os.getcwd()
        print("Model saved dir: ", cfg['saved_folder'])

        if 'crop' not in cfg['data']['images']:
            cfg['data']['images']['crop'] = False
        if 'H' not in cfg['data']:
            cfg['data']['H'] = 1
        cfg['data']['logs_folder'] = os.path.dirname(cfg['data']['pickle_fn']) 

    if cfg.agent.type in ['bcimage', 'bcimage_pre']:
        cfg['data']['images']['per_img_out'] = EMBEDDING_DIMS[cfg['agent']['vision_model']]
        if cfg.agent.type == 'bcimage_pre':
            # assume in_dim is without adding the image embedding dimensions
            cfg['data']['in_dim'] = cfg['data']['in_dim'] + cfg['data']['images']['per_img_out']

    print(OmegaConf.to_yaml(cfg, resolve=True))

    from dm_pour import DMWaterPouringEnv
    eval_env = DMWaterPouringEnv(has_viewer=False)
    vision_model_name = 'moco_conv5_robocloud'
    model = load_pvr_model(vision_model_name)[0]
    model = model.eval().to(cfg.training.device) ## assume this model is used in eval
    transforms = load_pvr_transforms(vision_model_name)[1]

    with open(os.path.join(os.getcwd(), 'hydra.yaml'), 'w') as f:
        f.write(OmegaConf.to_yaml(cfg, resolve=True))

    global_seeding(cfg.training.seed)
    print(hydra.utils.get_original_cwd(),cfg.data.pickle_fn)

    agent_name = cfg['saved_folder'].split('outputs/')[-1]
    flat_dict = {}
    for key in ['data', 'agent', 'training']:
        flat_dict.update(dict(cfg[key]))
    wandb.init(project="toto-bc", config=flat_dict)
    wandb.run.name = "{}".format(agent_name)

    try:
        with open(os.path.join(hydra.utils.get_original_cwd(), cfg.data.pickle_fn), 'rb') as f:
            data = pickle.load(f)
    except:
        print("\n***Pickle does not exist. Make sure the pickle is in the logs_folder directory.")
        raise

    #for path in data:
    #    path['observations'] = numpy.hstack([path['observations'], path['embeddings']]) # Assume 'observations' in the dataset doesn't contain img embeddings

    dset = FrankaDatasetTraj(data,
        logs_folder=cfg.data.logs_folder,
        subsample_period=cfg.data.subsample_period,
        im_h=cfg.data.images.im_h,
        im_w=cfg.data.images.im_w,
        obs_dim=cfg.data.in_dim,
        action_dim=cfg.data.out_dim,
        H=cfg.data.H,
        top_k=cfg.data.top_k,
        device=cfg.training.device,
        cameras=cfg.data.images.cameras,
        img_transform_fn=load_transforms(cfg),
        noise=cfg.data.noise,
        crop_images=cfg.data.images.crop)
    del data
    split_sizes = [int(len(dset) * 0.8), len(dset) - int(len(dset) * 0.8)]
    train_set, test_set = random_split(dset, split_sizes)

    num_workers = 0
    train_loader = DataLoader(train_set, batch_size=cfg.training.batch_size, shuffle=True, num_workers=num_workers, pin_memory=True)
    test_loader = DataLoader(test_set, batch_size=cfg.training.batch_size)
    agent, _ = init_agent_from_config(cfg, cfg.training.device, normalization=dset)
    train_metric, test_metric = baselines.Metric(), baselines.Metric()

    for epoch in range(cfg.training.epochs):
        acc_loss = 0.
        train_metric.reset()
        test_metric.reset()
        batch = 0
        for data in train_loader:
            for key in data:
                data[key] = data[key].to(cfg.training.device)
            agent.train(data)
            acc_loss += agent.loss
            train_metric.add(agent.loss.item())
            print('epoch {} \t batch {} \t train {:.6f}'.format(epoch, batch, agent.loss.item()), end='\r')
            batch += 1

        for data in test_loader:
            for key in data:
                data[key] = data[key].to(cfg.training.device)
            test_metric.add(agent.eval(data))

        eval_reward = _eval_agent(eval_env, agent, cfg.training.device, model, transforms, epoch)

        log.info('epoch {} \t train {:.6f} \t test {:.6f} \t eval {:.6f}'.format(epoch, train_metric.mean, test_metric.mean, eval_reward))
        log.info(f'Accumulated loss: {acc_loss}')
        if epoch % cfg.training.save_every_x_epoch == 0:
            agent.save(os.getcwd())

        wandb.log({"Train Loss": train_metric.mean, "Epoch": epoch})
        wandb.log({"Test Loss": test_metric.mean, "Epoch": epoch})
        wandb.log({"Acc Train Loss": acc_loss, "Epoch": epoch})

    agent.save(os.getcwd())
    log.info("Saved agent to {}".format(os.getcwd()))

if __name__ == '__main__':
    main()