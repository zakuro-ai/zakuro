docker build . -t zakuroai/zakuro
docker run  --rm -v $(pwd):/workspace -it zakuroai/zakuro
