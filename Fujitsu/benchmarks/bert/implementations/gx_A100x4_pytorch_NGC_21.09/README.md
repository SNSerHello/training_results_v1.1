## Steps to launch training on a single node

### FUJITSU PRIMERGY GX2460M1 (single node)
Launch configuration and system-specific hyperparameters for the PRIMERGY GX2460M1
multi node submission are in the following scripts:
* for the 1-node PRIMERGY GX2460M1 submission: `config_PG.sh`

Steps required to launch training on PRIMERGY GX2460M1:

1. Build the container:

```
docker build --pull -t <docker/registry>/mlperf-fujitsu:language_model .
docker push <docker/registry>/mlperf-fujitsu:language_model
```

2. Launch the training:

1-node PRIMERGY GX2460M1 training:

```
cd ../pytorch-fujitsu
source config_PG.sh
CONT=mlperf-fujitsu:language_model DATADIR=<path/to/datadir> DATADIR_PHASE2=<path/to/datadir_phase2> EVALDIR=<path/to/evaldir> CHECKPOINTDIR=<path/to/checkpointdir> CHECKPOINTDIR_PHASE1=<path/to/checkpointdir_phase1> ./run_with_docker.sh
```
