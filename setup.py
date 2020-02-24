from setuptools import setup

INSTALL_REQUIRES = (
  'gevent>=1.3.0',
  'thrift>=0.10.0',
  'kazoo>=2.5.0',
  'six>=1.13.0',
  'requests>=2.0.0'
)

TESTS_REQUIRES = (
  'responses>=0.10.9',
)

setup(
  name='scales-rpc',
  version='2.0.1',
  author='Steve Niemitz',
  author_email='sniemitz@twitter.com',
  url='https://www.github.com/steveniemitz/scales',
  description='A python RPC client stack',
  summary='A generic python RPC client framework.',
  license='MIT License',
  packages=['scales',
            'scales.http',
            'scales.kafka',
            'scales.loadbalancer',
            'scales.mux',
            'scales.pool',
            'scales.redis',
            'scales.thrift',
            'scales.thrifthttp',
            'scales.thriftmux'],
  install_requires=INSTALL_REQUIRES,
  extras_require={
    'tests': INSTALL_REQUIRES + TESTS_REQUIRES
  }
)
