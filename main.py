import os
import time
import pickle
import pathlib
import argparse
import itertools
import subprocess
import numpy as np
import pandas as pd
from tqdm import tqdm
from pprint import pprint
from matplotlib import pyplot
from datetime import datetime

from scm import CausalModel
import utils
import loadData
import loadModel

from sklearn.model_selection import GridSearchCV
from sklearn.linear_model import Ridge
from sklearn.kernel_ridge import KernelRidge
from sklearn.gaussian_process.kernels import WhiteKernel, ExpSineSquared

import GPy

from debug import ipsh

from random import seed
RANDOM_SEED = 54321
seed(RANDOM_SEED) # set the random seed so that the random permutations can be reproduced again
np.random.seed(RANDOM_SEED)

CODE_MODE = 'dev'
# CODE_MODE = 'eval'

NUM_TEST_SAMPLES = 4 if CODE_MODE == 'eval' else 10
NORM_TYPE = 2
GRID_SEARCH_BINS = 5 if CODE_MODE == 'eval' else 5
NUMBER_OF_MONTE_CARLO_SAMPLES = 100
LAMBDA_LCB = 1
SAVED_MACE_RESULTS_PATH_M0 = '/Users/a6karimi/dev/recourse/_minimum_distances_m0'
SAVED_MACE_RESULTS_PATH_M1 = '/Users/a6karimi/dev/recourse/_minimum_distances_m1'


@utils.Memoize
def loadDataset(dataset_class, increment_indices = True):

  def incrementIndices(old_indices):
    new_indices = ['x' + str(int(index[1:]) + 1) for index in old_indices]
    return new_indices

  dataset_obj = loadData.loadDataset(dataset_class, return_one_hot = True, load_from_cache = False)

  if increment_indices:

    # only change data_frame_kurz/attributes_kurz (data_frame_long/attributes_long
    # may contain non-numeric columns)
    replacement_dict = dict(zip(
      dataset_obj.getInputAttributeNames(),
      incrementIndices(dataset_obj.getInputAttributeNames()),
    ))

    # update data_frame_kurz
    dataset_obj.data_frame_kurz = dataset_obj.data_frame_kurz.rename(columns=replacement_dict)

    # update attributes_kurz
    old_attributes_kurz = dataset_obj.attributes_kurz
    new_attributes_kurz = {}

    for old_key, old_value in old_attributes_kurz.items():
      if old_key in replacement_dict.keys():
        new_key = replacement_dict[old_key]
        new_value = old_value
        new_value.attr_name_kurz = replacement_dict[old_key]
      else:
        new_key = old_key
        new_value = old_value
      new_attributes_kurz[new_key] = new_value

    dataset_obj.attributes_kurz = new_attributes_kurz

  return dataset_obj


@utils.Memoize
def loadClassifier(dataset_class, model_class, experiment_folder_name):
  return loadModel.loadModelForDataset(model_class, dataset_class, experiment_folder_name)


@utils.Memoize
def loadCausalModel(dataset_class, experiment_folder_name):
  scm = CausalModel({
    'x1': lambda         n_samples: np.random.normal(size=n_samples),
    'x2': lambda     x1, n_samples: x1 + 1 + np.random.normal(size=n_samples),
    'x3': lambda x1, x2, n_samples: np.sqrt(3) * x1 * (x2 ** 2) + np.random.normal(size=n_samples),
  })
  # scm = CausalModel({
  #   'x1': lambda         n_samples: np.random.normal(size=n_samples),
  #   'x2': lambda     x1, n_samples: x1 + 1,
  #   'x3': lambda x1, x2, n_samples: np.sqrt(3) * x1 * (x2 ** 2),
  #   'x4': lambda     x3, n_samples: x3 + 5,
  #   'x5': lambda x2, x4, n_samples: x2 + 5 * x4,
  # })
  scm.visualizeGraph(experiment_folder_name)
  return scm


def measureActionSetCost(dataset_obj, factual_instance, action_set, norm_type):
  # TODO: the cost should be measured in normalized space over all features
  #       pass in dataset_obj to get..
  deltas = []
  for key in action_set.keys():
    deltas.append(action_set[key] - factual_instance[key])
  return np.linalg.norm(deltas, norm_type)


def prettyPrintDict(my_dict):
  for key, value in my_dict.items():
    my_dict[key] = np.around(value, 2)
  return my_dict


def getPredictionBatch(dataset_obj, classifier_obj, causal_model_obj, instances_df):
  sklearn_model = classifier_obj
  return sklearn_model.predict(instances_df)


def getPrediction(dataset_obj, classifier_obj, causal_model_obj, instance):
  sklearn_model = classifier_obj
  prediction = sklearn_model.predict(np.array(list(instance.values())).reshape(1,-1))[0]
  assert prediction in {0, 1}, f'Expected prediction in {0,1}; got {prediction}'
  return prediction


def didFlip(dataset_obj, classifier_obj, causal_model_obj, factual_instance, counterfactual_instance):
  return \
    getPrediction(dataset_obj, classifier_obj, causal_model_obj, factual_instance) != \
    getPrediction(dataset_obj, classifier_obj, causal_model_obj, counterfactual_instance)


# See https://stackoverflow.com/a/25670697
def lambdaWrapper(new_value):
  return lambda *args: new_value


@utils.Memoize
def getStructuralEquation(dataset_obj, classifier_obj, causal_model_obj, node, recourse_type):

  if recourse_type == 'm0_true':

    if node == 'x1':
      return lambda n1: n1
    elif node == 'x2':
      return lambda x1, n2: x1 + 1 + n2
    elif node == 'x3':
      return lambda x1, x2, n3: np.sqrt(3) * x1 * (x2 ** 2) + n3

  # elif recourse_type == 'approx_deprecated':

  #   if node == 'x1':
  #     return lambda n1: n1
  #   elif node == 'x2':
  #     return lambda x1, n2: 1 * x1 + 1 + n2
  #   elif node == 'x3':
  #     return lambda x1, x2, n3: 5.5 * x1 + 3.5 * x2 - 0.1 + n3

  elif recourse_type == 'm1_alin':

    X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()
    X_all = X_train.append(X_test)
    param_grid = {"alpha": np.linspace(0,10,11)}

    if node == 'x1':

      return lambda n1: n1

    elif node == 'x2':

      model = GridSearchCV(Ridge(), param_grid=param_grid)
      model.fit(X_all[['x1']], X_all[['x2']])
      return lambda x1, n2: model.predict([[x1]])[0][0] + n2

    elif node == 'x3':

      model = GridSearchCV(Ridge(), param_grid=param_grid)
      model.fit(X_all[['x1', 'x2']], X_all[['x3']])
      return lambda x1, x2, n3: model.predict([[x1, x2]])[0][0] + n3

  elif recourse_type == 'm1_akrr':

    X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()
    X_all = X_train.append(X_test)
    param_grid = {"alpha": [1e0, 1e-1, 1e-2, 1e-3],
                  "kernel": [ExpSineSquared(l, p)
                             for l in np.logspace(-2, 2, 5)
                             for p in np.logspace(0, 2, 5)]}

    if node == 'x1':
      print(f'[INFO] Fitting KRR (parent: n/a; child: x1) may be very expensive, memoizing aftewards.')

      return lambda n1: n1

    elif node == 'x2':
      print(f'[INFO] Fitting KRR (parent: x1; child: x2) may be very expensive, memoizing aftewards.')

      model = GridSearchCV(KernelRidge(), param_grid=param_grid)
      model.fit(X_all[['x1']], X_all[['x2']])
      return lambda x1, n2: model.predict([[x1]])[0][0] + n2

    elif node == 'x3':
      print(f'[INFO] Fitting KRR (parent: x1, x2; child: x3) may be very expensive, memoizing aftewards.')

      model = GridSearchCV(KernelRidge(), param_grid=param_grid)
      model.fit(X_all[['x1', 'x2']], X_all[['x3']])
      return lambda x1, x2, n3: model.predict([[x1, x2]])[0][0] + n3


def sampleGP(dataset_obj):
  raise NotImplementedError


@utils.Memoize
def trainGP(dataset_obj, node):
  raise NotImplementedError
  # X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()
  # X_all = X_train.append(X_test)
  # X_all = X_all[:10]

  # if node == 'x1':

  #   return 'TODO: update'

  # elif node == 'x2':

  #   print(f'[INFO] Fitting GP (parent: x1; child: x2) may be very expensive, memoizing aftewards.')
  #   X = X_all[['x1']].to_numpy()
  #   Y = X_all[['x2']].to_numpy()
  #   kernel = GPy.kern.RBF(input_dim=X.shape[1], variance=1., lengthscale=1.)
  #   model = GPy.models.GPRegression(X, Y, kernel)
  #   model.optimize_restarts(parallel=True, num_restarts = 5)

  # elif node == 'x3':

  #   print(f'[INFO] Fitting GP (parent: x1, x2; child: x3) may be very expensive, memoizing aftewards.')
  #   X = X_all[['x1', 'x2']].to_numpy()
  #   Y = X_all[['x3']].to_numpy()
  #   kernel = GPy.kern.RBF(input_dim=X.shape[1], variance=1., lengthscale=1.)
  #   model = GPy.models.GPRegression(X, Y, kernel)
  #   model.optimize_restarts(parallel=True, num_restarts = 5)

  # K = kernel.K(X)
  # sigma_noise = np.array(model.Gaussian_noise.variance)

  # N = K.shape[0]
  # def noise_posterior_mean(K, sigma, Y):
  #   S = np.linalg.inv(K + sigma * np.eye(N))
  #   return sigma * np.dot(S, Y)

  # def noise_posterior_covariance(K, sigma):
  #   S = np.linalg.inv(K + sigma * np.eye(N))
  #   return  sigma * (np.eye(N) - sigma * S)

  # mu_post = noise_posterior_mean(K, sigma_noise, Y)
  # cov_post = noise_posterior_covariance(K, sigma_noise)
  # var_post = np.array([cov_post[i,i] for i in range(N)])
  # conf_post = 1.96 * np.sqrt(var_post)

  # mu_prior = np.zeros_like(mu_post)
  # var_prior = sigma_noise * np.ones_like(var_post)
  # conf_prior = 1.96 * np.sqrt(var_prior)

  # return 'TODO'


@utils.Memoize
def trainHIVAE(dataset_obj):
  print(f'[INFO] Training HI-VAE on complete data; this may be very expensive, memoizing aftewards.')
  X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()
  X_all = X_train.append(X_test)
  # X_all = X_train

  data_train_path = str(pathlib.Path().absolute()) + '/_tmp_hivae/data_train.csv'
  data_types_path = str(pathlib.Path().absolute()) + '/_tmp_hivae/data_types.csv'
  miss_train_path = str(pathlib.Path().absolute()) + '/_tmp_hivae/miss_train.csv'

  # generate _tmp_hivae/data_train.csv
  X_all.to_csv(data_train_path, index = False, header=False)

  # generate _tmp_hivae/data_types.csv
  # TODO: use dataset_obj to determine data types for hivae (tf, and perhaps pytorch)

  hivae_model = 'model_HIVAE_inputDropout'
  save_path = '_tmp_hivae/'
  training_setup = f'{hivae_model}_{dataset_obj.dataset_name}'
  subprocess.run([
    '/Users/a6karimi/dev/HI-VAE/_venv/bin/python',
    '/Users/a6karimi/dev/HI-VAE/main_scripts.py',
    '--model_name', f'{hivae_model}',
    '--batch_size', '100',
    '--epochs', '200',
    '--data_file', f'{data_train_path}',
    '--types_file', f'{data_types_path}',
    '--dim_latent_z', '2',
    '--dim_latent_y', '5',
    '--dim_latent_s', '1',
    '--save_path', save_path,
    '--save_file', training_setup,
    # '--miss_file', f'{miss_train_path}',
    '--debug_flag', '0',
  ])

  return True # this along with Memoize means that this function will only ever be executed once


def sampleHIVAE(dataset_obj, samples_df, node, parents):
  # counterfactual_instance keys are the conditioning + intervention set, therefore
  # we should create something like Missing1050_1.csv where rows are like this:
  # 10,1   \n   10,2   \n   10,3   \n   11,5   \n   11,13   \n   12,13

  X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()
  X_all = X_train.append(X_test)
  # X_all = X_all[:10]
  # X_all = X_test

  data_test_path = str(pathlib.Path().absolute()) + '/_tmp_hivae/data_test.csv'
  data_types_path = str(pathlib.Path().absolute()) + '/_tmp_hivae/data_types.csv'
  miss_test_path = str(pathlib.Path().absolute()) + '/_tmp_hivae/miss_test.csv'

  # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
  # Testing correctness of hi-vae against mean-imputation
  # X_all.to_csv(data_test_path, index = False, header=False)

  # with open(miss_test_path, 'w') as out_file:
  #   for sample_idx in np.random.randint(0, X_all.shape[0], 50):
  #     for feature_idx in np.random.randint(0, 3, 1):
  #       out_file.write(f'{sample_idx+1},{feature_idx+1}\n')
  # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

  # generate _tmp_hivae/data_test.csv:
  samples_df \
    .fillna(1e-10) \
    .to_csv(data_test_path, index = False, header=False)
  # Add some data after adding missing values (needed for hi-vae test time to work)
  X_all.to_csv(data_test_path, index = False, header=False, mode='a')

  # To sample from p(x_i | pa_i), all columns other than pa_i should be missing
  col_indices = [samples_df.columns.get_loc(col) for col in set.difference(set(samples_df.columns), parents)]
  row_indices = range(samples_df.shape[0])
  nan_indices = list(itertools.product(row_indices, col_indices))
  nan_indices = [(coord[0]+1, coord[1]+1) for coord in nan_indices]
  np.savetxt(miss_test_path, nan_indices, fmt="%d", delimiter=",")

  hivae_model = 'model_HIVAE_inputDropout'
  save_path = '_tmp_hivae/'
  training_setup = f'{hivae_model}_{dataset_obj.dataset_name}'
  subprocess.run([
    '/Users/a6karimi/dev/HI-VAE/_venv/bin/python',
    '/Users/a6karimi/dev/HI-VAE/main_scripts.py',
    '--model_name', f'{hivae_model}',
    '--batch_size', '1000000',
    '--epochs', '1',
    '--data_file', f'{data_test_path}',
    '--types_file', f'{data_types_path}',
    '--dim_latent_z', '2',
    '--dim_latent_y', '5',
    '--dim_latent_s', '1',
    '--save_path', save_path,
    '--save_file', training_setup,
    '--miss_file', f'{miss_test_path}',
    '--train', '0',
    '--restore', '1',
    '--debug_flag', '0',
  ])

  # Read from wherever est_data is saved
  reconstructed_data = pd.read_csv(
    f'_tmp_hivae/Results/{training_setup}/{training_setup}_data_reconstruction_samples.csv',
    names = X_all.columns,
  )
  reconstructed_data = reconstructed_data[:samples_df.shape[0]] # remove the fixed (unimputed) samples
  samples_df[node] = reconstructed_data[node]
  return samples_df

  # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
  # # Testing correctness of hi-vae against mean-imputation
  # all_reconstruct_error = []
  # all_mean_impute_error = []
  # for line in open(miss_test_path).readlines():
  #   sample_idx, feature_idx = line.strip().split(',')
  #   sample_idx = int(sample_idx) - 1
  #   feature_idx = int(feature_idx) - 1
  #   reconstruct_error = np.linalg.norm(X_test.iloc[sample_idx, feature_idx] - reconstructed_data.iloc[sample_idx, feature_idx])
  #   mean_impute_error = np.linalg.norm(X_test.iloc[sample_idx, feature_idx] - np.mean(X_train.iloc[:,feature_idx]))
  #   all_reconstruct_error.append(reconstruct_error)
  #   all_mean_impute_error.append(mean_impute_error)
  #   print(f'\tReconstruction Error: {reconstruct_error} vs. Mean Impute Error: {mean_impute_error}')

  # print('\n' + '-'*40 + f'\nAverage Reconstruction Error: {np.mean(all_reconstruct_error)} vs. Average Mean Impute Error: {np.mean(all_mean_impute_error)}')
  # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<


def computeCounterfactualInstance(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type):

  if not bool(action_set): # if action_set is empty, CFE = F
    return factual_instance


  structural_equations_new = dict(zip(factual_instance.keys(), [
    getStructuralEquation(dataset_obj, classifier_obj, causal_model_obj, node, recourse_type)
    for node in factual_instance.keys()
  ]))

  # Step 1. abduction: get value of noise variables
  # tip: pass in n* = 0 to structural_equations (lambda functions)
  noise_variables_new = dict(zip(factual_instance.keys(), [
    factual_instance[node] - structural_equations_new[node](
      *[factual_instance[node] for node in causal_model_obj.getParentsForNode(node)],
      0,
    )
    for node in factual_instance.keys()
  ]))

  # Step 2. action: update structural equations
  for key, value in action_set.items():
    node = key
    intervention_value = value
    structural_equations_new[node] = lambdaWrapper(intervention_value)
    # *args is used to allow for ignoring arguments that may be passed into this
    # function (consider, for example, an intervention on x2 which then requires
    # no inputs to call the second structural equation function, but we still pass
    # in the arugments a few lines down)

  # Step 3. prediction: compute counterfactual values starting from root node
  # CANNOT USE THE COMMENTED CODE BELOW; BECAUSE CF VALUES FOR X_i DEPENDs ON CF
  # VALUES OF PA_i, WHICH IS NOT DEFINED YET IN THE ONE-LINER
  # counterfactual_instance_new = dict(zip(factual_instance.keys(), [
  #   structural_equations_new[node](
  #     *[counterfactual_instance_new[node] for node in causal_model_obj.getParentsForNode(node)],
  #     noise_variables_new[node],
  #   )
  #   for node in factual_instance.keys()
  # ]))
  counterfactual_instance_new = {}
  for node in factual_instance.keys():
    counterfactual_instance_new[node] = structural_equations_new[node](
      *[counterfactual_instance_new[node] for node in causal_model_obj.getParentsForNode(node)],
      noise_variables_new[node],
    )

  return counterfactual_instance_new

  # # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

  # structural_equations = {}
  # structural_equations['x1'] = getStructuralEquation(dataset_obj, classifier_obj, causal_model_obj, 'x1', recourse_type)
  # structural_equations['x2'] = getStructuralEquation(dataset_obj, classifier_obj, causal_model_obj, 'x2', recourse_type)
  # structural_equations['x3'] = getStructuralEquation(dataset_obj, classifier_obj, causal_model_obj, 'x3', recourse_type)

  # # Step 1. abduction: get value of noise variables
  # # tip: pass in n* = 0 to structural_equations (lambda functions)
  # noise_variables = {}
  # noise_variables['x1'] = factual_instance['x1'] - structural_equations['x1'](0)
  # noise_variables['x2'] = factual_instance['x2'] - structural_equations['x2'](factual_instance['x1'], 0)
  # noise_variables['x3'] = factual_instance['x3'] - structural_equations['x3'](factual_instance['x1'], factual_instance['x2'], 0)

  # # Step 2. action: update structural equations
  # for key, value in action_set.items():
  #   node = key
  #   intervention_value = value
  #   structural_equations[node] = lambdaWrapper(intervention_value)
  #   # *args is used to allow for ignoring arguments that may be passed into this
  #   # function (consider, for example, an intervention on x2 which then requires
  #   # no inputs to call the second structural equation function, but we still pass
  #   # in the arugments a few lines down)

  # # Step 3. prediction: compute counterfactual values starting from root node
  # counterfactual_instance = {}
  # counterfactual_instance['x1'] = structural_equations['x1'](noise_variables['x1'])
  # counterfactual_instance['x2'] = structural_equations['x2'](counterfactual_instance['x1'], noise_variables['x2'])
  # counterfactual_instance['x3'] = structural_equations['x3'](counterfactual_instance['x1'], counterfactual_instance['x2'], noise_variables['x3'])

  # print(counterfactual_instance)
  # print(counterfactual_instance_new)

  # return counterfactual_instance

  # # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<


def getRecourseDistributionSample(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type, num_samples):

  if not bool(action_set): # if action_set is empty, CFE = F
    return pd.DataFrame(dict(zip(
      dataset_obj.getInputAttributeNames(),
      [num_samples * [factual_instance[node]] for node in dataset_obj.getInputAttributeNames()],
    )))

  counterfactual_template = dict.fromkeys(
    dataset_obj.getInputAttributeNames(),
    np.NaN,
  )

  # get intervention and conditioning sets
  intervention_set = set(action_set.keys())

  # intersection_of_non_descendents_of_intervened_upon_variables
  conditioning_set = set.intersection(*[
    causal_model_obj.getNonDescendentsForNode(node)
    for node in intervention_set
  ])

  # assert there is no intersection
  assert set.intersection(intervention_set, conditioning_set) == set()

  # set values in intervention and conditioning sets
  for node in conditioning_set:
    counterfactual_template[node] = factual_instance[node]

  for node in intervention_set:
    counterfactual_template[node] = action_set[node]

  samples_df = pd.DataFrame(dict(zip(
    dataset_obj.getInputAttributeNames(),
    [num_samples * [counterfactual_template[node]] for node in dataset_obj.getInputAttributeNames()],
  )))

  if recourse_type == 'm1_gaus':
    raise NotImplementedError

    # getGPSample(dataset_obj, )

  elif recourse_type == 'm2_true':

    scm_do = causal_model_obj.scm
    preassigned_nodes = samples_df.columns[~samples_df.isnull().all()]
    for node in samples_df.columns[~samples_df.isnull().all()]:
      scm_do = scm_do.do(node)

    samples_df = scm_do.sample(
      n_samples = num_samples,
      set_values = dict(zip(
        preassigned_nodes,
        samples_df[preassigned_nodes].T.to_numpy()
      )),
    )

  elif recourse_type == 'm2_hvae':

    # TODO: run once per dataset and cache/save in experiment folder? (read from cache if available)
    if not os.path.exists(f'_tmp_hivae/Saved_Networks/model_HIVAE_inputDropout_{dataset_obj.dataset_name}/checkpoint'):
      trainHIVAE(dataset_obj)

    # Simply traverse the graph in order, and populate nodes as we go!
    # IMPORTANT: do not use set(topo ordering); it sometimes changes ordering!
    for node in causal_model_obj.getTopologicalOrdering():
      # if variable value not yet set through intervention or conditioning
      if samples_df[node].isnull().values.any():
        parents = causal_model_obj.getParentsForNode(node)
        # Confirm parents columns are present in samples_df
        assert not samples_df.loc[:,list(parents)].isnull().values.any()
        print(f'Sampling HI-VAE from p({node} | {", ".join(parents)})')
        samples_df = sampleHIVAE(dataset_obj, samples_df, node, parents)


  # IMPORTANT: if for whatever reason, the columns change order (e.g., as seen in
  # scm_do.sample), reorder them as they are to be used as inputs to the fixed classifier
  samples_df = samples_df[dataset_obj.data_frame_kurz.columns[1:]]
  return samples_df


def isPointConstraintSatisfied(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type):
  return didFlip(dataset_obj, classifier_obj, causal_model_obj, factual_instance, computeCounterfactualInstance(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type))


def isDistrConstraintSatisfied(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type):
  monte_carlo_samples_df = getRecourseDistributionSample(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type, NUMBER_OF_MONTE_CARLO_SAMPLES)
  monte_carlo_predictions = getPredictionBatch(dataset_obj, classifier_obj, causal_model_obj, monte_carlo_samples_df.to_numpy())

  # IMPORTANT... WE ARE CONSIDERING {0,1} LABELS AND FACTUAL SAMPLES MAY BE OF
  # EITHER CLASS. THEREFORE, THE CONSTRAINT IS SATISFIED WHEN SIGNIFICANTLY
  # > 0.5 OR < 0.5 FOR A FACTUAL SAMPLE WITH Y = 0 OR Y = 1, RESPECTIVELY.

  expectation = np.mean(monte_carlo_predictions)
  variance = np.sum(np.power(monte_carlo_predictions - expectation, 2)) / (len(monte_carlo_predictions) - 1)

  if getPrediction(dataset_obj, classifier_obj, causal_model_obj, factual_instance) == 0:
    return expectation - LAMBDA_LCB * np.sqrt(variance) > 0.5 # NOTE DIFFERNCE IN SIGN OF STD
  else: # factual_prediction == 1
    return expectation + LAMBDA_LCB * np.sqrt(variance) < 0.5 # NOTE DIFFERNCE IN SIGN OF STD


def getValidDiscretizedActionSets(dataset_obj):
  # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
  # # TODO: should only discretize real-values variables
  # x1_possible_actions = list(np.around(np.linspace(dataset_obj.attributes_kurz['x1'].lower_bound, dataset_obj.attributes_kurz['x1'].upper_bound, GRID_SEARCH_BINS + 1), 2))
  # x2_possible_actions = list(np.around(np.linspace(dataset_obj.attributes_kurz['x2'].lower_bound, dataset_obj.attributes_kurz['x2'].upper_bound, GRID_SEARCH_BINS + 1), 2))
  # x3_possible_actions = list(np.around(np.linspace(dataset_obj.attributes_kurz['x3'].lower_bound, dataset_obj.attributes_kurz['x3'].upper_bound, GRID_SEARCH_BINS + 1), 2))
  # x1_possible_actions.append('n/a')
  # x2_possible_actions.append('n/a')
  # x3_possible_actions.append('n/a')

  # all_action_sets = []
  # for x1_action in x1_possible_actions:
  #   for x2_action in x2_possible_actions:
  #     for x3_action in x3_possible_actions:
  #       all_action_sets.append({
  #         'x1': x1_action,
  #         'x2': x2_action,
  #         'x3': x3_action,
  #       })
  # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

  possible_actions_per_node = []
  for node in dataset_obj.getInputAttributeNames():
    # TODO: should only discretize real-values variables
    tmp = list(
      np.around(
        np.linspace(
          dataset_obj.attributes_kurz[node].lower_bound,
          dataset_obj.attributes_kurz[node].upper_bound,
          GRID_SEARCH_BINS + 1
        ),
      2)
    )
    tmp.append('n/a')
    possible_actions_per_node.append(tmp)

  all_action_tuples = list(itertools.product(
    *possible_actions_per_node
  ))

  all_action_sets = [
    dict(zip(dataset_obj.getInputAttributeNames(), elem))
    for elem in all_action_tuples
  ]

  # Go through, and for any action_set that has a value = 'n/a', remove ONLY
  # THAT key, value pair, NOT THE ENTIRE action_set.
  valid_action_sets = []
  for action_set in all_action_sets:
    valid_action_sets.append({k:v for k,v in action_set.items() if v != 'n/a'})

  return valid_action_sets


def computeOptimalActionSet(dataset_obj, classifier_obj, causal_model_obj, factual_instance, recourse_type):

  # TODO: add option to select computation: brute-force, using MACE/MINT, or SGD

  if recourse_type in {'m0_true', 'm1_alin', 'm1_akrr'}:
    constraint_handle = isPointConstraintSatisfied
  elif recourse_type in {'m1_gaus', 'm2_true', 'm2_hvae'}:
    constraint_handle = isDistrConstraintSatisfied
  else:
    raise Exception(f'{recourse_type} not recognized.')

  valid_action_sets = getValidDiscretizedActionSets(dataset_obj)
  print(f'\t[INFO] Computing optimal `{recourse_type}`: grid searching over {len(valid_action_sets)} action sets...')

  min_cost = 1e10
  min_cost_action_set = {}
  for action_set in tqdm(valid_action_sets):
    if constraint_handle(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type):
      cost_of_action_set = measureActionSetCost(dataset_obj, factual_instance, action_set, NORM_TYPE)
      if cost_of_action_set < min_cost:
        min_cost = cost_of_action_set
        min_cost_action_set = action_set

  print(f'\t done.')

  return min_cost_action_set


def scatterDecisionBoundary(dataset_obj, classifier_obj, causal_model_obj, ax):
  assert len(factual_instance.keys()) == 3
  sklearn_model = classifier_obj
  fixed_model_w = sklearn_model.coef_
  fixed_model_b = sklearn_model.intercept_

  x_range = ax.get_xlim()[1] - ax.get_xlim()[0]
  y_range = ax.get_ylim()[1] - ax.get_ylim()[0]
  X = np.linspace(ax.get_xlim()[0] - x_range / 10, ax.get_xlim()[1] + x_range / 10, 10)
  Y = np.linspace(ax.get_ylim()[0] - y_range / 10, ax.get_ylim()[1] + y_range / 10, 10)
  X, Y = np.meshgrid(X, Y)
  Z = - (fixed_model_w[0][0] * X + fixed_model_w[0][1] * Y + fixed_model_b) / fixed_model_w[0][2]

  surf = ax.plot_wireframe(X, Y, Z, alpha=0.3)


def scatterDataset(dataset_obj, ax):
  assert len(factual_instance.keys()) == 3
  X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()
  X_train_numpy = X_train.to_numpy()
  X_test_numpy = X_test.to_numpy()
  y_train = y_train.to_numpy()
  y_test = y_test.to_numpy()
  number_of_samples_to_plot = 200
  for idx in range(number_of_samples_to_plot):
    color_train = 'black' if y_train[idx] == 1 else 'magenta'
    color_test = 'black' if y_test[idx] == 1 else 'magenta'
    ax.scatter(X_train_numpy[idx, 0], X_train_numpy[idx, 1], X_train_numpy[idx, 2], marker='s', color=color_train, alpha=0.2, s=10)
    ax.scatter(X_test_numpy[idx, 0], X_test_numpy[idx, 1], X_test_numpy[idx, 2], marker='o', color=color_test, alpha=0.2, s=15)


def scatterFactual(factual_instance, ax):
  assert len(factual_instance.keys()) == 3
  ax.scatter(
    factual_instance['x1'],
    factual_instance['x2'],
    factual_instance['x3'],
    marker='P',
    color='black',
    s=70
  )


def scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type, marker_type, ax):

  assert len(factual_instance.keys()) == 3

  if recourse_type in {'m0_true', 'm1_alin', 'm1_akrr'}:
    # point recourse

    point = computeCounterfactualInstance(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type)
    color_string = 'green' if didFlip(dataset_obj, classifier_obj, causal_model_obj, factual_instance, point) else 'red'
    ax.scatter(point['x1'], point['x2'], point['x3'], marker = marker_type, color=color_string, s=70)

  elif recourse_type in {'m1_gaus', 'm2_true', 'm2_hvae'}:
    # distr recourse

    samples_df = getRecourseDistributionSample(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type, 100)
    for sample_idx in range(samples_df.shape[0]):
      sample = samples_df.iloc[sample_idx].to_dict()
      color_string = 'green' if didFlip(dataset_obj, classifier_obj, causal_model_obj, factual_instance, sample) else 'red'
      ax.scatter(sample['x1'], sample['x2'], sample['x3'], marker = marker_type, color=color_string, alpha=0.1, s=30)

    mean_distr_samples = {
      'x1': np.mean(samples_df['x1']),
      'x2': np.mean(samples_df['x2']),
      'x3': np.mean(samples_df['x3']),
    }
    color_string = 'green' if didFlip(dataset_obj, classifier_obj, causal_model_obj, factual_instance, mean_distr_samples) else 'red'
    ax.scatter(mean_distr_samples['x1'], mean_distr_samples['x2'], mean_distr_samples['x3'], marker = marker_type, color=color_string, alpha=0.5, s=70)

  else:

    raise Exception(f'{recourse_type} not recognized.')


def experiment1(dataset_obj, classifier_obj, causal_model_obj):
  ''' compare M0, M1, M2 on one factual samples and one **fixed** action sets '''
  X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()
  factual_instance = X_test.iloc[0].T.to_dict()

  # iterative over a number of action sets and compare the three counterfactuals
  # action_set = {'x1': -3}
  action_set = {'x2': +1}
  # action_set = {'x3': +1}
  # action_set = {'x1': +2, 'x3': +1, 'x5': 3}
  # action_set = {'x0': +2, 'x2': +1}
  # action_set = {'x1': +2, 'x3': +1}

  print(f'FC: \t\t{factual_instance}')
  print(f'M0_true: \t{computeCounterfactualInstance(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, "true")}')
  print(f'M1_alin: \t{computeCounterfactualInstance(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, "m1_alin")}')
  # print(f'M1_akrr: \t{computeCounterfactualInstance(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, "m1_akrr")}')
  # print(f'M1_gaus: \n{getRecourseDistributionSample(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, "m1_gaus", 1)}')
  print(f'M2_true: \n{getRecourseDistributionSample(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, "m2_true", 10)}')
  print(f'M2_hvae: \n{getRecourseDistributionSample(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, "m2_hvae", 10)}')


def experiment2(dataset_obj, classifier_obj, causal_model_obj):
  ''' compare M0, M1, M2 on <n> factual samples and <n> **fixed** action sets '''
  X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()
  factual_instances_dict = X_test.iloc[:3].T.to_dict()
  action_sets = [ \
    {'x1': 1}, \
    {'x2': 1}, \
    {'x3': 1}, \
  ]

  fig = pyplot.figure()

  for index, (key, value) in enumerate(factual_instances_dict.items()):
    idx_sample = index
    factual_instance_idx = key
    factual_instance = value
    for idx_action, action_set in enumerate(action_sets):
      ax = pyplot.subplot(
        len(factual_instances_dict),
        len(action_sets),
        idx_sample * len(action_sets) + idx_action + 1,
        projection = '3d')
      scatterFactual(factual_instance, ax)
      scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, 'm0_true', '*', ax)
      scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, 'm1_alin', 's', ax)
      scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, 'n/a', '^', ax)
      scatterDecisionBoundary(dataset_obj, classifier_obj, causal_model_obj, ax)
      ax.set_xlabel('x1')
      ax.set_ylabel('x2')
      ax.set_zlabel('x3')
      ax.set_title(f'sample_{factual_instance_idx} \n do({prettyPrintDict(action_set)})', fontsize=8, horizontalalignment='left')
      ax.view_init(elev=15, azim=10)

      # for angle in range(0, 360):
      #   ax.view_init(30, angle)
      #   pyplot.draw()
      #   pyplot.pause(.001)

  pyplot.suptitle('Compare M0, M1, M2 on <n> factual samples and <n> **fixed** action sets.', fontsize=14)
  pyplot.show()


def experiment3(dataset_obj, classifier_obj, causal_model_obj):
  ''' compare M0, M1, M2 on <n> factual samples and <n> **computed** action sets '''
  X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()

  num_plot_rows = np.floor(np.sqrt(NUM_TEST_SAMPLES))
  num_plot_cols = np.ceil(NUM_TEST_SAMPLES / num_plot_rows)

  # Only focus on instances with h(x^f) = 0 and therfore h(x^cf) = 1
  factual_instances_dict = X_test.loc[y_test.index[y_test == 0]].iloc[:NUM_TEST_SAMPLES].T.to_dict()

  fig = pyplot.figure()

  for enumeration_idx, (key, value) in enumerate(factual_instances_dict.items()):
    factual_instance_idx = key
    factual_instance = value

    print(f'\n\n[INFO] Computing counterfactuals (M0, M1, M2) for factual instance #{enumeration_idx+1} / {NUM_TEST_SAMPLES} (id #{factual_instance_idx})...')

    ax = pyplot.subplot(
      num_plot_cols,
      num_plot_rows,
      enumeration_idx + 1,
      projection = '3d')

    # scatterDataset(dataset_obj, ax)

    scatterFactual(factual_instance, ax)

    m0_optimal_action_set = computeOptimalActionSet(dataset_obj, classifier_obj, causal_model_obj, factual_instance, 'm0_true')
    m11_optimal_action_set = computeOptimalActionSet(dataset_obj, classifier_obj, causal_model_obj, factual_instance, 'm1_alin')
    # m12_optimal_action_set = computeOptimalActionSet(dataset_obj, classifier_obj, causal_model_obj, factual_instance, 'm1_akrr')
    # m13_optimal_action_set = computeOptimalActionSet(dataset_obj, classifier_obj, causal_model_obj, factual_instance, 'm1_gaus')
    m21_optimal_action_set = computeOptimalActionSet(dataset_obj, classifier_obj, causal_model_obj, factual_instance, 'm2_true')
    m22_optimal_action_set = computeOptimalActionSet(dataset_obj, classifier_obj, causal_model_obj, factual_instance, 'm2_hvae')

    scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, m0_optimal_action_set, 'm0_true', '*', ax)
    scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, m11_optimal_action_set, 'm0_true', 's', ax) # show where the counterfactual will lie, when action set is computed using m1 but carried out in m0
    # scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, m12_optimal_action_set, 'm0_true', 'D', ax) # show where the counterfactual will lie, when action set is computed using m1 but carried out in m0
    # scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, m13_optimal_action_set, 'm1_gaus', 'p', ax)
    scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, m21_optimal_action_set, 'm2_true', '^', ax)
    scatterRecourse(dataset_obj, classifier_obj, causal_model_obj, factual_instance, m22_optimal_action_set, 'm2_hvae', 'v', ax)

    scatterDecisionBoundary(dataset_obj, classifier_obj, causal_model_obj, ax)
    ax.set_xlabel('x1')
    ax.set_ylabel('x2')
    ax.set_zlabel('x3')
    ax.set_title(
      f'sample_{factual_instance_idx} - {prettyPrintDict(factual_instance)}'
      f'\n m0_true action set: do({prettyPrintDict(m0_optimal_action_set)}); cost: {measureActionSetCost(dataset_obj, factual_instance, m0_optimal_action_set, NORM_TYPE):.2f}'
      f'\n m1_alin action set: do({prettyPrintDict(m11_optimal_action_set)}); cost: {measureActionSetCost(dataset_obj, factual_instance, m11_optimal_action_set, NORM_TYPE):.2f}'
      # f'\n m1_akrr action set: do({prettyPrintDict(m12_optimal_action_set)}); cost: {measureActionSetCost(dataset_obj, factual_instance, m12_optimal_action_set, NORM_TYPE):.2f}'
      # f'\n m1_gaus action set: do({prettyPrintDict(m13_optimal_action_set)}); cost: {measureActionSetCost(dataset_obj, factual_instance, m13_optimal_action_set, NORM_TYPE):.2f}'
      f'\n m2_true action set: do({prettyPrintDict(m21_optimal_action_set)}); cost: {measureActionSetCost(dataset_obj, factual_instance, m21_optimal_action_set, NORM_TYPE):.2f}'
      f'\n m2_hvae action set: do({prettyPrintDict(m22_optimal_action_set)}); cost: {measureActionSetCost(dataset_obj, factual_instance, m22_optimal_action_set, NORM_TYPE):.2f}'
    , fontsize=8, horizontalalignment='left')
    ax.view_init(elev=20, azim=-30)
    print('[INFO] done.')

    # for angle in range(0, 360):
    #   ax.view_init(30, angle)
    #   pyplot.draw()
    #   pyplot.pause(.001)

  pyplot.suptitle(f'Compare M0, M1, M2 on {NUM_TEST_SAMPLES} factual samples and **optimal** action sets.', fontsize=14)
  pyplot.show()


def experiment4(dataset_obj, classifier_obj, causal_model_obj, experiment_folder_name):
  ''' asserting fscores ~= for m2_true and m2_hvae '''
  X_train, X_test, y_train, y_test = dataset_obj.getTrainTestSplit()

  # Only focus on instances with h(x^f) = 0 and therfore h(x^cf) = 1
  factual_instances_dict = X_test.loc[y_test.index[y_test == 0]].iloc[:NUM_TEST_SAMPLES].T.to_dict()

  per_instance_results = {}

  for enumeration_idx, (key, value) in enumerate(factual_instances_dict.items()):
    factual_instance_idx = f'sample_{key}'
    factual_instance = value

    print(f'\n\n[INFO] Processing factual instance `{factual_instance_idx}` (#{enumeration_idx + 1} / {len(factual_instances_dict.keys())})...')

    per_instance_results[factual_instance_idx] = {}
    per_instance_results[factual_instance_idx]['action_sets'] = []
    per_instance_results[factual_instance_idx]['cate_true_pred_mean'] = []
    per_instance_results[factual_instance_idx]['cate_true_pred_var'] = []
    per_instance_results[factual_instance_idx]['cate_true_pred_fscore'] = []
    per_instance_results[factual_instance_idx]['cate_hivae_pred_mean'] = []
    per_instance_results[factual_instance_idx]['cate_hivae_pred_var'] = []
    per_instance_results[factual_instance_idx]['cate_hivae_pred_fscore'] = []

    valid_action_sets = getValidDiscretizedActionSets(dataset_obj)
    for action_set_idx, action_set in enumerate(valid_action_sets):

      print(f'\t[INFO] Processing action set `{str(action_set)}` (#{action_set_idx + 1} / {len(valid_action_sets)})...')

      per_instance_results[factual_instance_idx]['action_sets'].append(str(action_set))

      for recourse_type in {'m2_true', 'm2_hvae'}:

        print(f'\t\t[INFO] Computing f-score for `{recourse_type}`...')

        monte_carlo_samples_df = getRecourseDistributionSample(dataset_obj, classifier_obj, causal_model_obj, factual_instance, action_set, recourse_type, NUMBER_OF_MONTE_CARLO_SAMPLES)
        # print(monte_carlo_samples_df.head())
        monte_carlo_predictions = getPredictionBatch(dataset_obj, classifier_obj, causal_model_obj, monte_carlo_samples_df.to_numpy())

        # IMPORTANT... WE ARE CONSIDERING {0,1} LABELS AND FACTUAL SAMPLES MAY BE OF
        # EITHER CLASS. THEREFORE, THE CONSTRAINT IS SATISFIED WHEN SIGNIFICANTLY
        # > 0.5 OR < 0.5 FOR A FACTUAL SAMPLE WITH Y = 0 OR Y = 1, RESPECTIVELY.

        expectation = np.mean(monte_carlo_predictions)
        variance = np.sum(np.power(monte_carlo_predictions - expectation, 2)) / (len(monte_carlo_predictions) - 1)

        per_instance_results[factual_instance_idx][f'{recourse_type}_pred_mean'].append(np.around(expectation, 2))
        per_instance_results[factual_instance_idx][f'{recourse_type}_pred_var'].append(np.around(variance, 2))
        if getPrediction(dataset_obj, classifier_obj, causal_model_obj, factual_instance) == 0:
          per_instance_results[factual_instance_idx][f'{recourse_type}_pred_fscore'].append(expectation - LAMBDA_LCB * np.sqrt(variance)) # NOTE DIFFERNCE IN SIGN OF STD
        else: # factual_prediction == 1
          per_instance_results[factual_instance_idx][f'{recourse_type}_pred_fscore'].append(expectation + LAMBDA_LCB * np.sqrt(variance)) # NOTE DIFFERNCE IN SIGN OF STD

  merged_results = {}
  merged_results['cate_true_pred_mean'] = []
  merged_results['cate_true_pred_var'] = []
  merged_results['cate_true_pred_fscore'] = []
  merged_results['cate_hivae_pred_mean'] = []
  merged_results['cate_hivae_pred_var'] = []
  merged_results['cate_hivae_pred_fscore'] = []
  for factual_instance_idx, results in per_instance_results.items():
    merged_results['cate_true_pred_mean'].extend(results['cate_true_pred_mean'])
    merged_results['cate_true_pred_var'].extend(results['cate_true_pred_var'])
    merged_results['cate_true_pred_fscore'].extend(results['cate_true_pred_fscore'])
    merged_results['cate_hivae_pred_mean'].extend(results['cate_hivae_pred_mean'])
    merged_results['cate_hivae_pred_var'].extend(results['cate_hivae_pred_var'])
    merged_results['cate_hivae_pred_fscore'].extend(results['cate_hivae_pred_fscore'])

  fig, axs = pyplot.subplots(1, 3, sharey = True)
  bins = np.linspace(-1.5, 1.5, 40)
  axs[0].hist(np.subtract(merged_results['cate_true_pred_mean'], merged_results['cate_hivae_pred_mean']), bins, alpha=0.5)
  axs[0].set_title('cate mean: true - hivae', fontsize=8)
  axs[1].hist(np.subtract(merged_results['cate_true_pred_var'], merged_results['cate_hivae_pred_var']), bins, alpha=0.5)
  axs[1].set_title('cate var: true - hivae', fontsize=8)
  axs[2].hist(np.subtract(merged_results['cate_true_pred_fscore'], merged_results['cate_hivae_pred_fscore']), bins, alpha=0.5)
  axs[2].set_title('cate fscore: true - hivae', fontsize=8)

  fig.suptitle('comparing f-score: cate true vs. cate hivae')
  pyplot.savefig(f'{experiment_folder_name}/comparing_cate_true_cate_hivae.png')


def visualizeDatasetAndFixedModel(dataset_obj, classifier_obj, causal_model_obj):

  fig = pyplot.figure()
  ax = pyplot.subplot(1, 1, 1, projection='3d')

  scatterDataset(dataset_obj, ax)
  scatterDecisionBoundary(dataset_obj, classifier_obj, causal_model_obj, ax)

  ax.set_xlabel('x1')
  ax.set_ylabel('x2')
  ax.set_zlabel('x3')
  ax.set_title(f'datatset')
  # ax.legend()
  ax.grid(True)

  pyplot.show()


if __name__ == "__main__":

  parser = argparse.ArgumentParser()

  parser.add_argument(
    '-d', '--dataset_class',
    type = str,
    default = 'random',
    help = 'Name of dataset to train explanation model for: german, random, mortgage, twomoon')

  parser.add_argument(
    '-m', '--model_class',
    type = str,
    default = 'lr',
      help = 'Model class that will learn data: lr, mlp')

  parser.add_argument(
    '-p', '--process_id',
    type = str,
    default = '0',
    help = 'When running parallel tests on the cluster, process_id guarantees (in addition to time stamped experiment folder) that experiments do not conflict.')

  # parsing the args
  args = parser.parse_args()
  dataset_class = args.dataset_class
  model_class = args.model_class

  if not (dataset_class in {'random', 'mortgage', 'twomoon', 'german', 'credit', 'compass', 'adult'}):
    raise Exception(f'{dataset_class} not supported.')

  if not (model_class in {'lr', 'mlp'}):
    raise Exception(f'{model_class} not supported.')


  setup_name = f'{dataset_class}__{model_class}'
  experiment_folder_name = f"_experiments/{datetime.now().strftime('%Y.%m.%d_%H.%M.%S')}__{setup_name}"
  os.mkdir(f'{experiment_folder_name}')

  # only load once so shuffling order is the same
  dataset_obj = loadDataset(dataset_class)
  classifier_obj = loadClassifier(dataset_class, model_class, experiment_folder_name)
  causal_model_obj = loadCausalModel(dataset_class, experiment_folder_name)
  assert set(dataset_obj.getInputAttributeNames()) == set(causal_model_obj.getTopologicalOrdering())

  # experiment1(dataset_obj, classifier_obj, causal_model_obj)
  # experiment2(dataset_obj, classifier_obj, causal_model_obj)
  # experiment3(dataset_obj, classifier_obj, causal_model_obj)
  experiment4(dataset_obj, classifier_obj, causal_model_obj, experiment_folder_name)

  # sanity check
  # visualizeDatasetAndFixedModel(dataset_obj, classifier_obj, causal_model_obj)












































