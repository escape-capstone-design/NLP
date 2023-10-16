# -*- coding: utf-8 -*-
"""kosimcse-roberta.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1pMfZxDF6lFyH2ET0KTQk9cmF2fgK5xoB

# setting
"""

from google.colab import drive
drive.mount('/content/drive')

! pip install sentence_transformers datasets

# import

import random
import math
import numpy as np
import logging
from datetime import datetime
import pandas as pd
import os
import csv
from typing import List, Union
from tqdm.autonotebook import trange

import torch
from torch import nn
from torch.utils.tensorboard import SummaryWriter
from datasets import load_dataset
from torch.utils.data import Dataset, DataLoader, random_split
from sentence_transformers import SentenceTransformer, models, LoggingHandler, losses, util, SentencesDataset
from sentence_transformers.evaluation import SentenceEvaluator, TripletEvaluator
from sentence_transformers.readers import InputExample
from sentence_transformers.datasets import NoDuplicatesDataLoader

"""# dataset"""

# wa3i 프로젝트 데이터
wa3i_data = pd.read_csv('/content/drive/MyDrive/종설/dataset/science_wai.csv', usecols=['sentence1', 'sentence2', 'gold_label'])
wa3i_data = wa3i_data.dropna(how='any')
wa3i_data

# 직접 수집한 데이터
custom_data = pd.read_csv('/content/drive/MyDrive/종설/dataset/science_workbook.csv', usecols=['sentence1', 'sentence2', 'gold_label'])
custom_data = custom_data.dropna(how='any')
custom_data

# transform to Triplet format

def make_nli_triplet_input_example(dataset):
    train_data = {}

    def add_to_samples(sent1, sent2, label):
        if sent1 not in train_data:
            train_data[sent1] = {'contradiction': set(), 'entailment': set(), 'neutral': set()}
        train_data[sent1][label].add(sent2)

    for i, row in dataset.iterrows():
        sent1 = str(row['sentence1']).strip()
        sent2 = str(row['sentence2']).strip()
        label = row['gold_label'].strip()

        add_to_samples(sent1, sent2, label)

    # transform to InputExamples
    input_examples = []
    for sent1, others in train_data.items():
        if len(others['entailment']) > 0 and len(others['contradiction']) > 0:
            entailment_list = list(others['entailment'])
            contradiction_list = list(others['contradiction'])

            # Shuffle the lists to randomize the selection
            random.shuffle(entailment_list)
            random.shuffle(contradiction_list)

            # Use the same anchor for multiple triplets
            anchor = sent1

            for _ in range(5):  # Adjust the number of triplets as needed
                ent = random.choice(entailment_list)
                con = random.choice(contradiction_list)

                input_examples.append(InputExample(texts=[anchor, ent, con]))

    return input_examples

wa3i_dataset = make_nli_triplet_input_example(wa3i_data)
wa3i_dataset[0].texts

custom_dataset = make_nli_triplet_input_example(custom_data)
custom_dataset[0].texts

print(f'triplet wa3i: {len(wa3i_dataset)}')
print(f'triplet custom: {len(custom_dataset)}')

# (wa3i dataset) 8:2 split

dataset_size = len(wa3i_dataset)
wa3i_train_size = int(dataset_size * 0.8)
wa3i_validation_size = int(dataset_size * 0.2)
wa3i_test_size = dataset_size - wa3i_train_size - wa3i_validation_size

wa3i_train, wa3i_valid, wa3i_test = random_split(wa3i_dataset, [wa3i_train_size, wa3i_validation_size, wa3i_test_size])

print(f"Training Data Size : {len(wa3i_train)}")
print(f"Validation Data Size : {len(wa3i_valid)}")
print(f"Testing Data Size : {len(wa3i_test)}")

# (custom dataset) 8:2 split

dataset_size = len(custom_dataset)
custom_train_size = int(dataset_size * 0.8)
custom_validation_size = int(dataset_size * 0.2)
custom_test_size = dataset_size - custom_train_size - custom_validation_size

custom_train, custom_valid, custom_test = random_split(custom_dataset, [custom_train_size, custom_validation_size, custom_test_size])

print(f"Training Data Size : {len(custom_train)}")
print(f"Validation Data Size : {len(custom_valid)}")
print(f"Testing Data Size : {len(custom_test)}")

# merge dataset

train_dataset = wa3i_train + custom_train
validation_dataset = wa3i_valid + custom_valid
test_dataset = wa3i_test + custom_test
print(f"Training Data Size : {len(train_dataset)}")
print(f"Validation Data Size : {len(validation_dataset)}")
print(f"Testing Data Size : {len(test_dataset)}")

"""# evaluator"""

# Evaluator by train
train_evaluator = TripletEvaluator.from_input_examples(
    train_dataset,
    name="train-evaluator",
)

# Evaluator by validation
valid_evaluator = TripletEvaluator.from_input_examples(
    validation_dataset,
    name="valid-evaluator",
)

# Evaluator by test
test_evaluator = TripletEvaluator.from_input_examples(
    test_dataset,
    name="test-evaluator",
)

# get loss value

logger = logging.getLogger(__name__)


class LossEvaluator(SentenceEvaluator):

    def __init__(self, loader, loss_model: nn.Module = None, name: str = '', log_dir: str = None,
                 show_progress_bar: bool = False, write_csv: bool = True):

        """
        Evaluate a model based on the loss function.
        The returned score is loss value.
        The results are written in a CSV and Tensorboard logs.
        :param loader: Data loader object
        :param loss_model: loss module object
        :param name: Name for the output
        :param log_dir: path for tensorboard logs
        :param show_progress_bar: If true, prints a progress bar
        :param write_csv: Write results to a CSV file
        """

        self.loader = loader
        self.write_csv = write_csv
        self.logs_writer = SummaryWriter(log_dir=log_dir)
        self.name = name
        self.loss_model = loss_model

        # move model to gpu:  lidija-jovanovska
        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        loss_model.to(self.device)

        if show_progress_bar is None:
            show_progress_bar = (
                    logger.getEffectiveLevel() == logging.INFO or logger.getEffectiveLevel() == logging.DEBUG)
        self.show_progress_bar = show_progress_bar

        self.csv_file = "loss_evaluation" + ("_" + name if name else '') + "_results.csv"
        self.csv_headers = ["epoch", "steps", "loss"]

    def __call__(self, model, output_path: str = None, epoch: int = -1, steps: int = -1) -> float:

        self.loss_model.eval()

        loss_value = 0
        self.loader.collate_fn = model.smart_batching_collate
        num_batches = len(self.loader)
        data_iterator = iter(self.loader)

        with torch.no_grad():
          for _ in trange(num_batches, desc="Iteration", smoothing=0.05, disable=not self.show_progress_bar):
              sentence_features, labels = next(data_iterator)
              # move data to gpu
              for i in range(0, len(sentence_features)):
                  for key, value in sentence_features[i].items():
                      sentence_features[i][key] = sentence_features[i][key].to(self.device)
              labels = labels.to(self.device)
              loss_value += self.loss_model(sentence_features, labels).item()

        final_loss = loss_value / num_batches
        if output_path is not None and self.write_csv:

            csv_path = os.path.join(output_path, self.csv_file)
            output_file_exists = os.path.isfile(csv_path)

            with open(csv_path, newline='', mode="a" if output_file_exists else 'w', encoding="utf-8") as f:
                writer = csv.writer(f)
                if not output_file_exists:
                    writer.writerow(self.csv_headers)

                writer.writerow([epoch, steps, final_loss])

            # ...log the running loss
            self.logs_writer.add_scalar('val_loss',
                                        final_loss,
                                        steps)

        self.loss_model.zero_grad()
        self.loss_model.train()

        return final_loss

"""# model"""

# Load Embedding Model
embedding_model = models.Transformer(
    model_name_or_path="BM-K/KoSimCSE-roberta-multitask",
    max_seq_length=256,
    do_lower_case=True
)

# Only use Mean Pooling -> Pooling all token embedding vectors of sentence.
pooling_model = models.Pooling(
    embedding_model.get_word_embedding_dimension(),
    pooling_mode_mean_tokens=True,
    pooling_mode_cls_token=False,
    pooling_mode_max_tokens=False,
)

model = SentenceTransformer(modules=[embedding_model, pooling_model])

# config
num_epochs = 3
batch_size = 16

train_dataset = SentencesDataset(train_dataset, model=model)

# Train Dataloader
train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)

# train model
pretrained_model_name = "KoSimCSE-roberta"
model_save_path = 'output/' + pretrained_model_name.replace("/", "-")+'-'+datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

# Use ContrastiveLoss
train_loss = losses.TripletLoss(model)

# warmup steps
warmup_steps = math.ceil(len(train_dataset) * num_epochs / batch_size * 0.1) #10% of train data for warm-up
logging.info("Warmup-steps: {}".format(warmup_steps))

epochs = []
train_list = []
valid_list = []
test_list = []
train_loss_history = []
valid_loss_history = []

# loss evaluator
valid_loader = DataLoader(validation_dataset, batch_size=batch_size, shuffle=True)
valid_loss_evaluator = LossEvaluator(valid_loader, loss_model=train_loss, log_dir='logs/', name='valid')


# Training
for epoch in range(num_epochs):
    print(f'{epoch} epoch')
    epochs.append(epoch)
    model.fit(
              train_objectives=[(train_dataloader, train_loss)],
              evaluator=valid_evaluator,
              epochs=1,
              evaluation_steps=int(len(train_dataloader)*0.1),
              warmup_steps=warmup_steps,
              output_path=model_save_path,
              use_amp=False       #Set to True, if your GPU supports FP16 operations
    )

    train = train_evaluator(model)
    train_list.append(train)
    print(f'train => {train}')

    valid = valid_evaluator(model)
    valid_list.append(valid)
    print(f'valid => {valid}')

    # test = test_evaluator(model)
    # test_list.append(test)
    # print(f'test => {test}')

    # train_loss = train_evaluator(model)
    # train_loss_history.append(train_loss)
    # print(f'train_loss => {train_loss}')

    valid_loss = valid_loss_evaluator(model)
    valid_loss_history.append(valid_loss)
    print(f'valid_loss => {valid_loss}')

"""# result"""

# evaluation valid
valid_evaluator(model, output_path=model_save_path)

# evaluation test
# test_evaluator(model, output_path=model_save_path)

import matplotlib.pyplot as plt

plt.figure(figsize=(10,5))
plt.subplot(1,2,2)
plt.xlabel('Epoch')
plt.ylabel('Performance')
plt.plot(epochs, train_list, label='train')
plt.plot(epochs, valid_list, label='valid')
plt.legend()
plt.show()

plt.figure(figsize=(10,5))
plt.subplot(1,2,2)
plt.xlabel('Epoch')
plt.ylabel('Loss')
plt.plot(epochs, train_loss_history, label='train')
plt.plot(epochs, valid_loss_history, label='valid')
plt.legend()
plt.show()

# 문장을 정의
sentence1 = "주위의 온도가 높아진다."
sentence2 = "온도는 상승한다."

# 두 문장을 모델로 임베딩
embeddings = model.encode([sentence1, sentence2], convert_to_tensor=True)

# 코사인 유사도 계산
cosine_score = util.pytorch_cos_sim(embeddings[0], embeddings[1])

print(f"첫 번째 문장과 두 번째 문장의 코사인 유사도: {cosine_score.item():.4f}")

# 문장을 정의
sentence1 = "주위의 온도가 높아진다."
sentence2 = "온도는 상승하지 않는다."

# 두 문장을 모델로 임베딩
embeddings = model.encode([sentence1, sentence2], convert_to_tensor=True)

# 코사인 유사도 계산
cosine_score = util.pytorch_cos_sim(embeddings[0], embeddings[1])

print(f"첫 번째 문장과 두 번째 문장의 코사인 유사도: {cosine_score.item():.4f}")