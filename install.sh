#!/bin/bash
#
# Developed by CADIC Jean Maximilien
# Contact contact@zakuro.ai
#
do_install(){
        mkdir -p /opt/zakuro
        mkdir -p /opt/zakuro/bin
        wget https://raw.githubusercontent.com/zakuro-ai/zakuro/master/docker-compose.yml -O /opt/zakuro/docker-compose.yml
        wget https://raw.githubusercontent.com/zakuro-ai/zakuro/master/setup -O /opt/zakuro/Dockerfile
        wget https://raw.githubusercontent.com/zakuro-ai/zakuro/master/Makefile -O /opt/zakuro/Makefile
        echo "cd /opt/zakuro;make Makefile \$@" > /opt/zakuro/bin/zakuro_cli
        chmod +x /opt/zakuro/bin/zakuro_cli
}

do_install
PATH=$PATH:/opt/zakuro/bin
