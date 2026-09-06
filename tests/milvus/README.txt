Milvus docker安装
bash standalone_embed.sh start

会挂载本地目录到容器

restart)
    stop
    start
    ;;
start)
    start
    ;;
stop)
    stop
    ;;
upgrade)
    upgrade
    ;;
delete)
    delete
    ;;

Attu docker安装
docker run -d --name attu \
  -p 3000:3000 \
  -e MILVUS_ADDRESS=host.docker.internal:19530 \
  -v attu-data:/data \
  zilliz/attu:v3.0.0

