from .metrics      import performance
from .utils        import find_wavelength
from .meta         import get_sensor_bands
from .transformers import TransformerPipeline, LogTransformer

from importlib import import_module
from functools import partial
from pathlib   import Path 

import numpy as np
import pkgutil, warnings, sys, os


def get_methods(wavelengths, sensor, product, debug=True, allow_opt=False, **kwargs):
	''' Retrieve all benchmark functions from the appropriate product
		directory. Import each function with "model" in the function
		name, ensure any necessary parameters have a default value 
		available, and test whether the function can be run with the
		given wavelengths. A template folder for new algorithms is 
		available in the Benchmarks directory.
	'''
	methods = {}
	sensor  = sensor.split('-')[0] # remove any extra data which is used by the MDN

	# Iterate over all benchmark algorithm folders in the appropriate product directory
	for (_, name, is_folder) in pkgutil.iter_modules([Path(__file__).parent.resolve().joinpath('Benchmarks', product)]):
		if is_folder:
			module   = Path(__file__).parent.stem
			imported = import_module(f'{module}.Benchmarks.{product}.{name}.model')
			for function in dir(imported):

				# Check all functions which have "model" in their name
				if 'model' in function: 
					model = getattr(imported, function)
					if hasattr(model, 'has_default'):
						if model.has_default or allow_opt:

							# Add a set of dummy parameters to check if wavelengths are valid for the method
							if not model.has_default:
								kwargs.update( dict(zip(model.opt_vars, [1]*len(model.opt_vars))) )

							try: 
								out = model(np.ones((1, len(wavelengths))), wavelengths, sensor, **kwargs)
								if out is not None:
									model_name          = getattr(model, 'model_name', name)
									methods[model_name] = partial(model, sensor=sensor)
									methods[model_name].__name__ = model_name 
							except Exception as e: 
								if debug:
									print(f'Exception for function {name}: {e}')

						# Models which don't have default parameters available can only be run through bench_opt	
						elif debug:
							print(name,'requires optimization')
	return methods


def bench_product(args, sensor, x_test, y_test=None, slices=None, silent=True, product='chl', method=None):
	assert(silent or y_test is not None), 'Must provide y values or set silent=True'

	waves = get_sensor_bands(sensor, args)
	assert(x_test.shape[1] <= len(waves)), f'Too many features given as bands for {sensor}: {x_test.shape[1]} vs {len(waves)}'

	methods = get_methods(waves, sensor, product, tol=15)
	assert(method is None or method in methods), f'Unknown algorithm "{method}". Options are: \n{list(methods.keys())}'

	ests = []
	lbls = []
	for name, func in methods.items():
		if method is None or name == method:
			est_val = func(x_test, waves, tol=15)
			
			if product == 'chl':
				est_val[~np.isfinite(est_val)] = 0
				est_val[est_val < 0] = 0

			if not silent:
				curr_slice = slices
				if slices is None:
					assert(y_test.shape[1] == est_val.shape[1]), 'Ambiguous y data provided - need to give slices parameter.'
					curr_slice = {product:slice(None)}

				ins_val = y_test[:, curr_slice[product]]
				for i in range(ins_val.shape[1]):
					print( performance(name, ins_val[:, i], est_val) )
			ests.append(est_val)
			lbls.append(name)
	return dict(zip(lbls, ests))
	

def bench_opt(args, sensor, x_train, x_test, y_train, y_test, slices, silent=False, product='chl'):
	waves   = np.array(get_sensor_bands(sensor))
	methods = get_methods(waves, sensor, product, allow_opt=True)

	ests = []
	lbls = []
	for name, method in methods.items():
		method.fit(x_train, y_train, waves)
		est_chl = method.predict(x_test)

		if not silent:
			ins_val = y_test[:, slices[product]]
			for i in range(ins_val.shape[1]):
				print( performance(name.split('_')[0], ins_val[:, i], est_chl) )
		ests.append(est_chl)
		lbls.append(name+'_opt')
	return dict(zip(lbls, ests))


def bench_ml(args, sensor, x_train, y_train, x_test, y_test, slices=None, silent=False, product='chl', x_other=None, 
			bagging=True, gridsearch=False, scale=True):
	from sklearn.preprocessing import RobustScaler, QuantileTransformer, MinMaxScaler
	from sklearn.model_selection import GridSearchCV
	from sklearn.ensemble import BaggingRegressor
	from sklearn import gaussian_process, svm, neural_network, kernel_ridge, neighbors
	from xgboost import XGBRegressor as XGB
	from sklearn.exceptions import ConvergenceWarning
	warnings.simplefilter("always", ConvergenceWarning)

	# gridsearch=False
	methods = {
		'XGB' : {
			'class'   : XGB,
			'default' : {'max_depth': 15, 'n_estimators': 50, 'objective': 'reg:squarederror'},
			'grid'    : {
				'n_estimators' : [10, 50, 100],
				'max_depth'    : [5, 15, 30],
				'objective'    : ['reg:squarederror'],
			}},
		'SVM' : {
			'class'   : svm.SVR,
			'default' : {'C': 100.0, 'gamma': 'scale', 'kernel': 'rbf'},
			'grid'    : {
				'kernel' : ['rbf', 'poly'],
				'gamma'  : ['auto', 'scale'],
				'C'      : [1e-1, 5e-1, 1e0, 5e0, 1e1, 5e1, 1e2],
			}},
		'MLP' : {
			'class'   : neural_network.MLPRegressor,
			'default' : {'alpha': 1e-05, 'hidden_layer_sizes': [100, 100, 100, 100], 'learning_rate': 'constant'},
			'grid'    : {
				'hidden_layer_sizes' : [[100]*i for i in range(1, 6)],
				'alpha'              : [1e-5, 1e-4, 1e-3, 1e-2],
				'learning_rate'      : ['constant', 'adaptive'],
			}},
		'KNN' : {
			'class'   : neighbors.KNeighborsRegressor,
			'default' : {'n_neighbors': 5, 'p':1},
			'grid'    : {
				'n_neighbors' : [3, 5, 10, 20],
				'p'           : [1, 2, 3],
			}},
		# 'KRR' : {
		# 	'class'   : kernel_ridge.KernelRidge,
		# 	'default' : {'alpha': 1e1, 'kernel': 'laplacian'},
		# 	'grid'    : {
		# 		'alpha' : [1e-1, 1e0, 1e1, 1e2],
		# 		'kernel': ['rbf', 'laplacian', 'linear'],
		# 	}},
		# 'GPR' : {
		# 	'class'   : gaussian_process.GaussianProcessRegressor,
		# 	'default' : {'kernel': gaussian_process.kernels.ConstantKernel(1.0, (1e-1, 1e3)) * gaussian_process.kernels.RBF(1.0, (1e-1, 1e3))},
		# 	'grid'    : {
		# 		'kernel' : [gaussian_process.kernels.ConstantKernel(1.0, (1e-1, 1e3)) * gaussian_process.kernels.RBF(1.0, (1e-1, 1e3)), 
		# 					gaussian_process.kernels.ConstantKernel(10.0, (1e-1, 1e3)) * gaussian_process.kernels.RBF(10.0, (1e-1, 1e3))],
		# 	}},
	}

	# Filter MLP convergence warnings
	if not sys.warnoptions:
		warnings.simplefilter('ignore')
		os.environ['PYTHONWARNINGS'] = 'ignore'

	if False:
		x_train = x_train[:,:len(get_sensor_bands(sensor, args))]
		x_test  = x_test[:,:len(get_sensor_bands(sensor, args))]

	if scale:
		x_scaler = RobustScaler()
		y_scaler = TransformerPipeline([LogTransformer()])#, MinMaxScaler((-1, 1))]) 
		x_scaler.fit(x_train)
		y_scaler.fit(y_train)
		x_train = x_scaler.transform(x_train)
		x_test  = x_scaler.transform(x_test)
		y_train = y_scaler.transform(y_train)

	if slices is None:
		assert(y_test.shape[1] == y_train.shape[1]), 'Ambiguous y data provided - need to give slices parameter.'
		slices = {product:slice(None)}

	with warnings.catch_warnings():
		warnings.filterwarnings('ignore')
		if gridsearch:
			print('\nPerforming gridsearch...')

		other = []
		ests  = []
		lbls  = []
		for method, params in methods.items():
			if gridsearch:
				model = GridSearchCV(params['class'](), params['grid'], refit=False, n_jobs=3, scoring='neg_median_absolute_error')
				model.fit(x_train.copy(), y_train.copy().flatten())

				print(f'Best {method} params: {model.best_params_}')
				model = params['class'](**model.best_params_)

			else:
				model = params['class'](**params['default'])

			if bagging:
				model = BaggingRegressor(model, n_estimators=10, max_samples=0.75, bootstrap=False)
			model.fit(x_train.copy(), y_train.copy().flatten())
			est_val = model.predict(x_test.copy())

			if scale:
				est_val = y_scaler.inverse_transform(est_val[:,None]).flatten()	

			if not silent:
				ins_val = y_test[:, slices[product]]
				for i in range(ins_val.shape[1]):
					print( performance(method, ins_val[:, i], est_val) )

			ests.append(est_val)
			lbls.append(method)

			if x_other is not None:
				est = model.predict(x_other.copy())
				if scale: est = y_scaler.inverse_transform(est[:,None]).flatten()
				other.append(est)

	if not len(other):
		return dict(zip(lbls, ests))
	return dict(zip(lbls, ests)), dict(zip(lbls, other))


def bench_qaa(args, sensor, x_test, y_test, slices, silent=False):
	from .QAA import QAA
	waves = np.array(get_sensor_bands(sensor, args))
	param = [k for k in slices if k[0]=='a' or k[0]=='b']
	qaa   = QAA(x_test, waves[:x_test.shape[1]])
	ests  = []
	lbls  = []

	for name in param:
		if name.replace('_','') in qaa:
			ins_val = y_test[:, slices[name]]
			est_val = qaa[name.replace('_','')]
			assert(ins_val.shape == est_val.shape), [ins_val.shape, est_val.shape]

			for i in range(ins_val.shape[1]):
				if not silent:
					print( performance('QAA %s%i' % (name, waves[i]), ins_val[:, i], est_val[:, i]) )
				ests.append(est_val[:,i])
				lbls.append('QAA %s%s' % (name, waves[i]))
		else:
			print(list(qaa.keys()))
			assert(0), f"{name.replace('_','')} not found in QAA outputs"
	return dict(zip(lbls, ests))


def bench_giop(args, sensor, x_test, y_test, slices, silent=False):
	from .GIOP.giop import GIOP
	waves = np.array(get_sensor_bands(sensor, args))
	param = [k for k in slices if k[0]=='a' or k[0]=='b' or k[0]=='c']
	gest  = GIOP(x_test, waves[:x_test.shape[1]], sensor)
	ests  = []
	lbls  = []

	for name in param:
		if name.replace('_','') in gest:
			ins_val = y_test[:, slices[name]]
			est_val = gest[name.replace('_','')]
			assert(ins_val.shape == est_val.shape), [ins_val.shape, est_val.shape]

			for i in range(ins_val.shape[1]):
				lbl = 'GIOP %s%s' % (name, waves[i] if 'chl' not in name else '')
				if not silent:
					print( performance(lbl, ins_val[:, i], est_val[:, i]) )
				ests.append(est_val[:,i])
				lbls.append(lbl)
		else:
			assert(0), f'{name} not found in GIOP outputs'
	return dict(zip(lbls, ests))


def bench_iop(*args, **kwargs):
	iop_ests = {}
	iop_ests.update( bench_qaa(*args, **kwargs) )
	# iop_ests.update( bench_giop(*args, **kwargs) )
	return iop_ests


def bench_tss(args, sensor, x_test, y_test, slices, silent=False):
	waves = np.array(get_sensor_bands(sensor))[:x_test.shape[1]]
	A665  = 355.85
	B665  = 1.74
	C665  = 1728 
	R665  = x_test[:, find_wavelength(665, waves)]
	tss_val = (B665 + (A665 * np.pi * R665) / (1 - np.pi * R665 / C665)).flatten()[:,None] 
	ins_val = y_test[:, slices['tss']]
	for i in range(ins_val.shape[1]):
		if not silent: print( performance('Nechad', ins_val[:, i], tss_val) )
	return {'Nechad TSS': tss_val}


def bench_chl(*args, **kwargs):
	return bench_product(*args, product='chl', **kwargs)


def run_benchmarks(args, sensor, x_test, y_test, slices, silent=True, x_train=None, y_train=None, gridsearch=False, with_ml=False):
	benchmarks = {}
	if len([k for k in slices if k[0] in ['a','b'] and '*' not in k and 'ad' not in k]):
		benchmarks.update( bench_qaa(args, sensor, x_test, y_test, slices, silent) )

		# if 'aph' in slices or 'apg' in slices:
		# 	benchmarks.update( bench_giop(args, sensor, x_test, y_test, slices, silent) )

	if 'chl' in slices:
		benchmarks.update( bench_chl(args, sensor, x_test, y_test, slices, silent) )
		# benchmarks.update( bench_giop(sensor, x_test, y_test, slices, silent) )
		if with_ml:
			benchmarks.update( bench_ml(args, sensor, x_train, y_train, x_test, y_test, slices, silent, gridsearch=gridsearch) )


	if 'tss' in slices:
		# benchmarks.update( bench_tss(args, sensor, x_test, y_test, slices, silent) )
		benchmarks.update( bench_product(args, sensor, x_test, y_test, slices, silent, product='tss') )

	return benchmarks


def print_benchmarks(*args, **kwargs):
	run_benchmarks(*args, silent=False, **kwargs)
