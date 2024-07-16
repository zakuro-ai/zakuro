# PHONY are targets with no files to check, all in our case
.DEFAULT_GOAL := help
include .env

# Extract the recommender version
VERSION=$(shell cat ${PKG_NAME}/version)
BUILD=$(shell date)


#########################################################################################
################# MISC ##################################################################
#########################################################################################
# Display the current version
help:
	@echo ${PKG_NAME} v$(VERSION)
	@echo "Usage: make {build,  bash, ...}"
	@echo "Please check README.md for instructions"
	@echo ""

# Build the project
.PHONY: add_build
add_build: 
	echo ${BUILD} > ${PKG_NAME}/build

# Build the project
.PHONY: build
build: add_build build_wheel build_docker

# Run the project
.PHONY: run
run:
	docker compose down
	docker compose up ${ENV} -d

# Build the project's wheels
.PHONY: build_wheel
build_wheel: 
	# Build the wheels
	@mkdir -p dist/legacy;
	@mv dist/*.whl dist/legacy/ || true; \
	pip install build && python -m build --wheel;
	rsbuild clean

# Build the docker image
.PHONY: build_docker
build_docker: 
	# Build the wheels
	docker compose build

# Build launch and connect
.PHONY: all
all: 
	docker compose up -d
	docker exec -it zakuro bash