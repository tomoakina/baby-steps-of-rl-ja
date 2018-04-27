import random
import argparse
from collections import deque
import numpy as np
import tensorflow as tf
from tensorflow.python import keras as K
from tensorflow.python.keras._impl.keras.models import clone_model
from PIL import Image
import gym
import gym_ple
from fn_framework import FNAgent, Trainer, Observer


class DeepQNetworkAgent(FNAgent):

    def __init__(self, epsilon, actions, test_mode=False):
        super().__init__(epsilon, actions)
        self.test_mode = test_mode
        self._teacher_model = None

    def initialize(self, experiences, optimizer):
        feature_shape = experiences[0].s.shape
        if self.test_mode:
            self.make_test_model(feature_shape)
            self.model.compile(optimizer, loss="mse")
        else:
            self.make_model(feature_shape)
            self.model.compile(optimizer, loss=tf.losses.huber_loss)
        self.initialized = True
        print("Done initialize. From now, begin training!")

    def make_model(self, feature_shape):
        model = K.Sequential()
        model.add(K.layers.Conv2D(
            32, kernel_size=8, strides=4, padding="same",
            input_shape=feature_shape, kernel_initializer="normal",
            activation="relu"))
        model.add(K.layers.Conv2D(
            64, kernel_size=4, strides=2, padding="same",
            kernel_initializer="normal",
            activation="relu"))
        model.add(K.layers.Conv2D(
            64, kernel_size=3, strides=1, padding="same",
            kernel_initializer="normal",
            activation="relu"))
        model.add(K.layers.Flatten())
        model.add(K.layers.Dense(256, kernel_initializer="normal",
                                 activation="relu"))
        model.add(K.layers.Dense(len(self.actions),
                                 kernel_initializer="normal"))
        self.model = model
        self._teacher_model = clone_model(self.model)

    def make_test_model(self, feature_shape):
        model = K.Sequential()
        model.add(K.layers.Dense(64, input_shape=feature_shape,
                                 activation="relu"))
        model.add(K.layers.Dense(len(self.actions), activation="relu"))
        self.model = model
        self._teacher_model = clone_model(self.model)

    def estimate(self, state):
        return self.model.predict(np.array([state]))[0]

    def update(self, experiences, gamma):
        states = np.array([e.s for e in experiences])
        n_states = np.array([e.n_s for e in experiences])

        estimateds = self.model.predict(states)
        future = self._teacher_model.predict(n_states)

        for i, e in enumerate(experiences):
            reward = e.r
            if not e.d:
                reward += gamma * np.max(future[i])
            estimateds[i][e.a] = reward

        loss = self.model.train_on_batch(states, estimateds)
        return loss

    def update_teacher(self):
        self._teacher_model.set_weights(self.model.get_weights())


class CatcherObserver(Observer):

    def __init__(self, env, width, height, frame_count):
        super().__init__(env)
        self.width = width
        self.height = height
        self.frame_count = frame_count
        self._frames = deque(maxlen=frame_count)

    def transform(self, state):
        grayed = Image.fromarray(state).convert("L")
        resized = grayed.resize((self.width, self.height))
        resized = np.array(resized).astype("float")
        normalized = resized / 255.0  # scale to 0~1
        if len(self._frames) == 0:
            for i in range(self.frame_count):
                self._frames.append(normalized)
        else:
            self._frames.append(normalized)
        feature = np.array(self._frames)
        # Convert the feature shape (f, w, h) => (w, h, f)
        feature = np.transpose(feature, (1, 2, 0))

        return feature


class DeepQNetworkTrainer(Trainer):

    def __init__(self, buffer_size=50000, batch_size=32,
                 gamma=0.99, initial_epsilon=0.1, final_epsilon=1e-3,
                 learning_rate=1e-3, teacher_update_freq=5, report_interval=10,
                 log_dir="", file_name=""):
        super().__init__(buffer_size, batch_size, gamma,
                         report_interval, log_dir)
        self.file_name = file_name if file_name else "dqn_agent.h5"
        self.initial_epsilon = initial_epsilon
        self.final_epsilon = final_epsilon
        self.learning_rate = learning_rate
        self.teacher_update_freq = teacher_update_freq
        self.training_count = 0
        self.training_episode = 0
        self.loss = 0
        self.callback = K.callbacks.TensorBoard(self.log_dir)

    def train(self, env, episode_count=3000, render=False, test_mode=False):
        actions = list(range(env.action_space.n))
        agent = DeepQNetworkAgent(1.0, actions, test_mode)
        self.training_count = 0
        self.training_episode = episode_count

        self.train_loop(env, agent, episode_count, render)
        agent.save(self.make_path(self.file_name))
        return agent

    def episode_begin(self, episode, agent):
        self.loss = 0

    def buffer_full(self, episode, agent):
        optimizer = K.optimizers.Adam(lr=self.learning_rate)
        agent.initialize(self.experiences, optimizer)
        self.callback.set_model(agent.model)
        self.training_episode -= episode
        agent.epsilon = self.initial_epsilon

    def step(self, episode, step_count, agent, experience):
        if agent.initialized:
            batch = random.sample(self.experiences, self.batch_size)
            self.loss += agent.update(batch, self.gamma)

    def episode_end(self, episode, step_count, agent):
        reward = sum([e.r for e in self.get_recent(step_count)])
        self.loss = self.loss / step_count
        self.reward_log.append(reward)
        if agent.initialized:
            self.write_log(self.training_count, self.loss, reward)
            if self.is_event(self.training_count, self.report_interval):
                agent.save(self.make_path(self.file_name))
            if self.is_event(self.training_count, self.teacher_update_freq):
                agent.update_teacher()

            diff = (self.initial_epsilon - self.final_epsilon)
            decay = diff / self.training_episode
            agent.epsilon = max(agent.epsilon - decay, self.final_epsilon)
            self.training_count += 1

        if self.is_event(episode, self.report_interval):
            recent_rewards = self.reward_log[-self.report_interval:]
            desc = self.make_desc("reward", recent_rewards)
            print("At episode {}, {}".format(episode, desc))

    def write_log(self, index, loss, score):
        for name, value in zip(("loss", "score"), (loss, score)):
            summary = tf.Summary()
            summary_value = summary.value.add()
            summary_value.simple_value = value
            summary_value.tag = name
            self.callback.writer.add_summary(summary, index)
            self.callback.writer.flush()


def main(play, is_test):
    trainer = DeepQNetworkTrainer(file_name="dqn_agent.h5")
    path = trainer.make_path(trainer.file_name)

    if is_test:
        print("Train on test mode")
        obs = gym.make("CartPole-v0")
    else:
        env = gym.make("Catcher-v0")
        obs = CatcherObserver(env, 80, 80, 4)
        trainer.learning_rate = 9e-4
        trainer.initial_epsilon = 0.2

    if play:
        agent = DeepQNetworkAgent.load(obs, path)
        agent.play(obs, render=True)
    else:
        trainer.train(obs, test_mode=is_test)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DQN Agent")
    parser.add_argument("--play", action="store_true",
                        help="play with trained model")
    parser.add_argument("--test", action="store_true",
                        help="train by test mode")

    args = parser.parse_args()
    main(args.play, args.test)
