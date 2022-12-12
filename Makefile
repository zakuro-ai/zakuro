.DEFAULT_GOAL := build

ENDPOINT=10.13.13.2:8786

# Build
build: 
	@docker-compose down
	@docker rmi -f zakuroai/agent
	@docker build . -t zakuroai/agent --no-cache

down:
	@docker-compose down

up:
	@docker-compose up -d

reset:
	@zakuro_cli down
	@zakuro_cli up
	@docker exec -d zakuro_agent  /bin/bash -c "dask worker ${ENDPOINT}"

	
ssh:
	@docker exec -e HOME=/home/foo -u foo -it zakuro_agent bash

# Sandbox
sandbox:
	@zakuro_cli reset
	@zakuro_cli ssh

nb:
	@zakuro_cli reset
	@docker exec -e HOME=/home/foo -u foo -d zakuro_agent /bin/bash -c "jupyter lab --ip 0.0.0.0 --port 8890 /workspace"
	@zakuro_cli ssh
