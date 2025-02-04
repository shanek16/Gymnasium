import os
import sys
current_file_path = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_file_path)
desired_path = os.path.expanduser("~/Project/PERSISTENT/Gymnasium")
sys.path.append(desired_path)
import numpy as np
import random
from gymnasium import Env
from gymnasium.spaces import Box, Dict, Discrete, MultiDiscrete
from typing import Optional
import rendering

from mdp import Actions, States
from numpy import arctan2, array, cos, pi, sin
from PIL import Image, ImageDraw, ImageFont

def wrap(theta):
    if theta > pi:
        theta -= 2 * pi
    elif theta < -pi:
        theta += 2 * pi
    return theta

class MUMT(Env):
    '''
    ver 1: 
    - if initial # of uavs, targets don't change,
    - include all uav-target pairs as observation for value comparison
    '''
    metadata = {"render_modes": ["human", "rgb_array"], "render_fps": 30}
    class UAV:
        def __init__(
            self,
            state,
            v=1.0,
            battery=3000,
            ):
            self.v = v
            self.dt = 0.05
            self.state = state
            self.battery = battery
            self.charging = 0
        
        def copy(self):
            # Create a new UAV instance with the same attributes
            return MUMT.UAV(state=self.state.copy(), v=self.v, battery=self.battery)
    
        def move(self, action):
            dtheta = action * self.dt
            _lambda = dtheta / 2
            if _lambda == 0.0:
                self.state[0] += self.v*self.dt * cos(self.state[-1])
                self.state[1] += self.v*self.dt * sin(self.state[-1])
            else:
                ds = self.v*self.dt * sin(_lambda) / _lambda
                self.state[0] += ds * cos(self.state[-1] + _lambda)
                self.state[1] += ds * sin(self.state[-1] + _lambda)
                self.state[2] += dtheta
                self.state[2] = wrap(self.state[2])
        @property
        def obs(self): # observation of uav relative to charging station
            x, y = self.state[:2]
            r = np.sqrt(x**2 + y**2)
                        # beta                  # theta
            alpha = wrap(arctan2(y, x) - wrap(self.state[-1]) - pi)
            beta = arctan2(y, x)
            return array([r, alpha, beta], dtype=np.float32)  # beta

    class Target:
        def __init__(
            self,
            state,
            age = 0,
            ):
            self.state = state
            self.surveillance = None
            self.age = age

        def copy(self):
            # Create a new Target instance with the same attributes
            return MUMT.Target(state=self.state.copy(), age=self.age)
        
        def cal_age(self):
            if self.surveillance == 0: # uav1 is not surveilling
                self.age = min(1000, self.age + 1) #changeage
            else:
                self.age = 0
        @property
        def obs(self): # polar coordinate of a target
            x, y = self.state
            r = np.sqrt(x**2 + y**2)
            beta = arctan2(y, x)
            return array([r, beta], dtype=np.float32)  # beta

    def __init__(
        self,
        render_mode: Optional[str] = None,
        r_max=80,
        r_min=0,
        dt=0.05,
        d=10.0,
        l=3, # noqa
        m=2, # of uavs
        n=2, # of targets
        r_c=3,
        max_step=6000,
        seed = None # one circle 1200 time steps
    ):
        super().__init__()
        self.render_mode = render_mode
        self.seed = seed
        # Create the observation space
        obs_space = {}

        # Add observation spaces for each UAV-target pair according to the rule
        for uav_id in range(1, m + 1):
            for target_id in range(1, n + 1):
                key = f"uav{uav_id}_target{target_id}"
                obs_space[key] = Box(low=np.float32([r_min, -np.pi]),
                                        high=np.float32([r_max, np.pi]),
                                        dtype=np.float32)

        # Add observation spaces for each UAV-charging station
        for uav_id in range(1, m + 1):
            key = f"uav{uav_id}_charge_station"
            obs_space[key] = Box(low=np.float32([r_min, -np.pi]),
                                 high=np.float32([r_max, np.pi]),
                                 dtype=np.float32)

        # Add observation space for battery and age
        # Assuming one battery value per UAV and one age value per target
        obs_space["battery"] = Box(low=np.float32([0]*m),
                                   high=np.float32([3000]*m),
                                   dtype=np.float32)
        obs_space["age"] = Box(low=np.float32([0]*n),
                               high=np.float32([1000]*n),
                               dtype=np.float32)

        self.observation_space = Dict(obs_space)
        self.action_space = MultiDiscrete([n + 1] * m, seed=self.seed)
        self.dt = dt
        self.discount = 0.999
        self.d = d  # target distance
        self.l = l  # coverage gap: coverage: d-l ~ d+l # noqa
        self.m = m  # of uavs
        self.n = n  # of targets
        self.uavs = []
        self.uav_color = [(random.randrange(0, 11) / 10, random.randrange(0, 11) / 10, random.randrange(0, 11) / 10) for _ in range(m)]
        self.targets = []
        self.r_c = r_c  # charge station radius
        self.step_count = None
        self.font = ImageFont.truetype("/usr/share/fonts/truetype/freefont/FreeMono.ttf", 20)
        self.num2str = {0: "charge", 1: "target_1"}
        self.max_step = max_step
        self.viewer = None
        self.SAVE_FRAMES_PATH = f"../../../../visualized/{self.m}U{self.n}T"
        self.episode_counter = 0
        self.frame_counter = 0
        self.save_frames = False
        self.action = None

        # initialization for Dynamic Programming
        self.n_r = 800
        self.n_alpha = 360
        self.n_u = 2 #21

        current_file_path = os.path.dirname(os.path.abspath(__file__))
        self.distance_keeping_result00 = np.load(current_file_path+ os.path.sep + "v1_80_2a_dkc_val_iter.npz")
        self.distance_keeping_straightened_policy00 = self.distance_keeping_result00["policy"] # .data
        self.time_optimal_straightened_policy00 = np.load(current_file_path+ os.path.sep + "v1_terminal_40+40_2a_toc_policy_fp64.npy")

        self.states = States(
            np.linspace(0.0, 80.0, self.n_r, dtype=np.float32),
            np.linspace(
                -np.pi,
                np.pi - np.pi / self.n_alpha,
                self.n_alpha,
                dtype=np.float32,
            ),
            cycles=[np.inf, np.pi * 2],
        )

        self.actions = Actions(
            np.linspace(-1.0 / 4.5, 1.0 / 4.5, self.n_u, dtype=np.float32).reshape(
                (-1, 1)
            )
        )

    def reset(
        self,
        uav_pose=None,
        target_pose=None,
        batteries=None,
        ages=None,
        seed: Optional[int] = None,
        options: Optional[dict] = None,
    ):
        self.uavs = []
        self.targets = []
        np.random.seed(seed)
        self.episode_counter += 1
        self.step_count = 0
        if self.save_frames:
            os.makedirs(
                os.path.join(self.SAVE_FRAMES_PATH, f"{self.episode_counter:03d}"),
                exist_ok=True,
            )
            self.frame_counter = 0
        if uav_pose is None:
            uav_r = np.random.uniform(0, 40, self.m)  # D=40
            uav_beta = np.random.uniform(-pi, pi, self.m)
            uav_theta = np.random.uniform(-pi, pi, self.m)
            # Create the state arrays
            uav_x = uav_r * np.cos(uav_beta)
            uav_y = uav_r * np.sin(uav_beta)

            # Stack them into a single array
            uav_states = np.vstack([uav_x, uav_y, uav_theta]).T  # Transpose to get the correct shape
        else:
            uav_states = uav_pose
        if batteries is None:
            batteries = np.random.randint(1500, 3000, self.m)
        else:
            batteries = batteries
        # Create UAV instances
        for i in range(self.m):
            self.uavs.append(self.UAV(state=uav_states[i], battery=batteries[i]))

        if target_pose is None:
            target1_r = np.random.uniform(20, 35, self.n)  # 0~ D-d
            target1_beta = np.random.uniform(-np.pi, np.pi, self.n)
            target_states = np.array([target1_r * np.cos(target1_beta), target1_r * np.sin(target1_beta)]).T
            ages = [0] * self.n
        else:
            target_states, ages = target_pose  # Assuming target_pose is an iterable of target states
        # Create Target instances
        for i in range(self.n):
            self.targets.append(self.Target(state=target_states[i], age=ages[i]))
        return self.dict_observation, {}

    def toc_get_action(self, state):
        S, P = self.states.computeBarycentric(state)
        action = sum(p * self.actions[int(self.time_optimal_straightened_policy00[s])] for s, p in zip(S, P))
        return action

    def dkc_get_action(self, state):
        S, P = self.states.computeBarycentric(state)
        action = sum(p * self.actions[int(self.distance_keeping_straightened_policy00[s])] for s, p in zip(S, P))
        return action

    def control_uav(self, uav_idx, action):
        self.uavs[uav_idx].charging = 0
        if self.uavs[uav_idx].battery <= 0: # UAV dead
            pass
        else: # UAV alive: can take action
            if action == 0:  # go to charging station
                if (self.uavs[uav_idx].obs[0] < self.r_c):
                    # uav1 no move
                    self.uavs[uav_idx].charging = 1
                    self.uavs[uav_idx].battery = min(self.uavs[uav_idx].battery + 10, 3000)
                else:  # not able to land on charge station(too far)
                    self.uavs[uav_idx].battery -= 1
                    w1_action = self.toc_get_action(self.uavs[uav_idx].obs[:2])
                    self.uavs[uav_idx].move(w1_action)
            else:  # surveil target1
                self.uavs[uav_idx].battery -= 1
                w1_action = self.dkc_get_action(self.rel_observation(uav_idx, action-1)[:2])
                self.uavs[uav_idx].move(w1_action)

    def cal_surveillance(self, uav_idx, target_idx):
        if self.uavs[uav_idx].battery <= 0:
            return 0
        else: # UAV alive
            if (
                self.d - self.l < self.rel_observation(uav_idx, target_idx)[0] < self.d + self.l
                and self.uavs[uav_idx].charging != 1 # uav 1 is not charging(on the way to charge is ok)
            ):
                return 1 # uav1 is surveilling target 1
            else:
                return 0

    def step(self, action):
        self.action = action
        terminal = False
        truncated = False
        action = np.squeeze(action)
        reward = 0
        if action.ndim == 0:
            action = np.expand_dims(action, axis=0)
        for uav_idx, uav_action in enumerate(action):
            self.control_uav(uav_idx, uav_action)

        surveillance_matrix = np.zeros((self.m, self.n))
        for uav_idx in range(self.m):
            for target_idx in range(self.n):
                surveillance_matrix[uav_idx, target_idx] = self.cal_surveillance(uav_idx, target_idx)
        surveillance = np.any(surveillance_matrix, axis=0).astype(int)
        for target_idx in range(self.n):
            self.targets[target_idx].surveillance = surveillance[target_idx]
            self.targets[target_idx].cal_age()
            reward += -self.targets[target_idx].age
        reward = reward / self.n # average reward of all targets
        if self.save_frames and int(self.step_count) % 6 == 0:
            image = self.render(mode="rgb_array")
            path = os.path.join(
                self.SAVE_FRAMES_PATH,
                f"{self.episode_counter:03d}",
                f"{self.frame_counter+1:04d}.bmp",
            )
            image = Image.fromarray(image)
            '''setup text label here'''
            image.save(path)
            self.frame_counter += 1
        self.step_count += 1
        if self.step_count >= self.max_step:
            truncated = True
        return self.dict_observation, reward, terminal, truncated, {}

    def dry_cal_surveillance(self, uav1_copy, target1_copy, r_t):
        if uav1_copy.battery <= 0: # UAV dead
            target1_copy.surveillance = 0
        else: # UAV alive
            if (self.d - self.l < r_t < self.d + self.l
                and uav1_copy.charging != 1):
                target1_copy.surveillance = 1
            else:
                target1_copy.surveillance = 0
        return target1_copy.surveillance

    def dry_step(self, uav_idx, target_idx, action, future, discount):
        # Copying relevant instance variables
        uav1_copy = self.uavs[uav_idx].copy()
        target1_copy = self.targets[target_idx].copy()
        step_count_copy = self.step_count

        terminal = False
        truncated = False
        action = np.squeeze(action)
        reward = 0

        dry_dict_observation = {}
        dry_dict_observation['uav1_target1'] = self.rel_observation(uav_idx, target_idx)[:2]
        dry_dict_observation['uav1_charge_station'] = uav1_copy.obs[:2]
        dry_dict_observation = self.dict_observation.copy()
        for i in range(future):
            if truncated:
                break
            # Logic for UAV1's battery and actions
            uav1_copy.charging = 0
            if uav1_copy.battery <= 0:  # UAV dead
                pass
            else:
                if action == 0:
                    if (dry_dict_observation['uav1_charge_station'][0] < self.r_c):
                        uav1_copy.charging = 1
                        uav1_copy.battery = min(uav1_copy.battery + 10, 3000)
                    else:
                        uav1_copy.battery -= 1
                        w1_action = self.toc_get_action(dry_dict_observation['uav1_charge_station'][:2])
                        uav1_copy.move(w1_action)
                    
                else:
                    uav1_copy.battery -= 1
                    w1_action = self.dkc_get_action(dry_dict_observation['uav1_target1'][:2])
                    uav1_copy.move(w1_action)
            uav_x, uav_y, theta = uav1_copy.state
            target_x, target_y = target1_copy.state
            x = target_x - uav_x
            y = target_y - uav_y
            r_t = np.sqrt(x**2 + y**2)
            beta_t = arctan2(y, x)
            alpha_t = wrap(beta_t - wrap(theta))
            self.dry_cal_surveillance(uav1_copy, target1_copy, r_t)
            target1_copy.cal_age()

            step_count_copy += 1
            if step_count_copy >= self.max_step:
                truncated = True

            dry_dict_observation = { # is this state s_{t+10}?: Yes it is
                # r, alpha
                "uav1_target1": np.array([r_t, alpha_t], dtype=np.float32,),
                "uav1_charge_station": np.array([uav1_copy.obs[0], uav1_copy.obs[1]], dtype=np.float32,),
                "battery":  np.float32(uav1_copy.battery),
                "age": target1_copy.age,
                # "previous_action": action
            }
            reward += -target1_copy.age*discount**i
        return dry_dict_observation, reward, terminal, truncated, {}

    def render(self, mode="human"):
        if self.viewer is None:
            self.viewer = rendering.Viewer(1000, 1000)
            bound = int(40 * 1.05)
            self.viewer.set_bounds(-bound, bound, -bound, bound)

        # Render all self.targets
        for target_idx, target in enumerate(self.targets):
            target_x, target_y = target.state
            outer_donut = self.viewer.draw_circle(
                radius=self.d + self.l, x=target_x, y=target_y, filled=True
            )
            if target.surveillance == 1:
                outer_donut.set_color(0.6, 0.6, 1.0, 0.3)  # lighter
            else:
                outer_donut.set_color(0.3, 0.3, 0.9, 0.3)  # transparent blue
            inner_donut = self.viewer.draw_circle(
                radius=self.d - self.l, x=target_x, y=target_y, filled=True
            )
            inner_donut.set_color(0, 0, 0)  # erase inner part
            circle = self.viewer.draw_circle(
                radius=self.d, x=target_x, y=target_y, filled=False
            )
            circle.set_color(1, 1, 1)
            target_circle = self.viewer.draw_circle(
                radius=1, x=target_x, y=target_y, filled=True
            )
            if target_idx + 1 in self.action:
                try:
                    target_circle.set_color(*self.uav_color[int(np.where(self.action == target_idx + 1)[0])])  # yellow
                except:
                    target_circle.set_color(1, 1, 0)  # multiple uavs are after one target
            else:
                target_circle.set_color(1, 0.6, 0)  # orange

        # charge station @ origin
        charge_station = self.viewer.draw_circle(radius=self.r_c, filled=True)
        
        if 0 in self.action:
            try:
                charge_station.set_color(*self.uav_color[int(np.where(self.action == 0)[0])])  # yellow
            except:
                charge_station.set_color(1, 1, 0)  # multiple uavs are after the charge station
            for uav_idx, uav in enumerate(self.uavs):
                if uav.charging == 1:
                    charge_station.set_color(1,0,0)
                    break
        else:
            charge_station.set_color(0.1, 0.9, 0.1)  # green            

        # Render all self.uavs
        for uav_idx, uav in enumerate(self.uavs):
            if uav.battery <= 0:  # UAV dead
                continue
            uav_x, uav_y, uav_theta = uav.state
            uav_transform = rendering.Transform(translation=(uav_x, uav_y), rotation=uav_theta)
            uav_tri = self.viewer.draw_polygon([(-0.8, 0.8), (-0.8, -0.8), (1.6, 0)])
            try:
                uav_tri.set_color(*self.uav_color[uav_idx])
                uav_tri.add_attr(uav_transform)
            except:
                print('len(self.uav_color): ',len(self.uav_color))
                print('uav_idx: ', uav_idx)
                input()

        return self.viewer.render(return_rgb_array=mode == "rgb_array")

    # @property
    def rel_observation(self, uav_idx, target_idx): # of target relative to uav
        uav_x, uav_y, theta = self.uavs[uav_idx].state
        target_x, target_y = self.targets[target_idx].state
        x = target_x - uav_x
        y = target_y - uav_y
        r = np.sqrt(x**2 + y**2)
        beta = arctan2(y, x)
        alpha = wrap(beta - wrap(theta))
        return array([r, alpha, beta],dtype=np.float32)

    @property
    def dict_observation(self):
        dictionary_obs = {}
        # Add observations for UAV-target pairs according to the rule
        for uav_id in range(self.m):
            for target_id in range(self.n):
                key = f"uav{uav_id+1}_target{target_id+1}"
                dictionary_obs[key] = self.rel_observation(uav_id, target_id)[:2]

        # Add observations for each UAV-charging station
        for uav_id in range(self.m):
            key = f"uav{uav_id+1}_charge_station"
            dictionary_obs[key] = self.uavs[uav_id].obs[:2]

        # Add observation for battery levels and ages of targets
        dictionary_obs["battery"] = np.float32([self.uavs[uav_id].battery for uav_id in range(self.m)])
        dictionary_obs["age"] = np.float32([self.targets[target_id].age for target_id in range(self.n)])

        return dictionary_obs


if __name__ == "__main__":
    m=2
    n=2
    uav_env = MUMT(m=m, n=n)

    # Number of features
    state_sample = uav_env.observation_space.sample()
    action_sample = uav_env.action_space.sample()
    print("state_sample: ", state_sample)
    print("action_sample: ", action_sample)
    print('uav_env.observation_space:', uav_env.observation_space)
    print('uav_env.action_space.n: ', uav_env.action_space)

    # testing env: alternating action
    # target1_r = np.random.uniform(20, 35, self.n)  # 0~ D-d
    # target1_beta = np.random.uniform(-np.pi, np.pi, self.n)
    target_states = np.array([np.array([0, 24]), np.array([30, 20])]).T
    ages = [100] * n
    batteries = np.array([3000, 1000])

    obs, _ = uav_env.reset(target_pose=(target_states, ages), batteries=batteries)
    step = 0
    while step < 5000:
        step += 1
        if step % 1000 == 0:
            action_sample = uav_env.action_space.sample()
        obs, reward, _, truncated, _ = uav_env.step(action_sample)
        bat = obs['battery']
        print(f'step: {step} | battery: {bat} | reward: {reward}')
        uav_env.render()
    
    # testing env: heuristic policy
    '''repitition = 10
    avg_reward = 0
    for i in range(repitition):
        step = 0
        truncated = False
        obs, _ = uav_env.reset(seed=i)
        bat = obs['battery']
        age = obs['age']
        total_reward = 0
        while truncated == False:
            step += 1
            action = np.arange(1, m + 1)
            action = np.where(action > n, 0, action)
            print(obs) # {'uav1_target1': array([36.854134 , -1.4976969], dtype=float32), 'uav2_target1': array([37.672398 ,  3.0869958], dtype=float32), 'uav1_charge_station': array([21.95254  , -2.0162203], dtype=float32), 'uav2_charge_station': array([28.607574 ,  2.5069222], dtype=float32), 'battery': array([2099., 2594.], dtype=float32), 'age': array([0.], dtype=float32)}
            # print each observation
            for key, value in obs.items():
                print(f'{key}: {value}')
            input()
            if bat > 2000:
                action = 1
            elif bat > 1000:
                # previous_action = obs['previous_action']
                if age == 0 or age > 800: # uav was surveilling
                # if previous_action:
                    action = 1
                else: # uav was charging
                    action = 0
            else:
                action = 0
            
            obs, reward, _, truncated, _ = uav_env.step(action)
            total_reward += reward
            bat = obs['battery']
            age = obs['age']
            # print(f'step: {step} | battery: {bat} | reward: {reward}') #, end=' |')
            # print(f'action: {action}')#, end=' |')
            # uav_env.print_q_value()
            # uav_env.render(mode='rgb_array')
        print(f'{i}: {total_reward}')   
        avg_reward += total_reward
    avg_reward /= repitition
    print(f'average reward: {avg_reward}')'''