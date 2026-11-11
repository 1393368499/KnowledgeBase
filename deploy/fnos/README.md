# 飞牛部署

部署目录：`/vol1/1000/knowledgebase`。网页访问地址：`http://192.168.222.6:8800`。

`compose.yaml` 使用迁移时的现有镜像文件系统。镜像被重新打包为单层，以减少本机飞牛逐层落盘的开销；应用源码没有随迁移更改。
镜像导入保留用户、工作目录、环境、入口和停止信号，方法见 [Docker import 文档](https://docs.docker.com/reference/cli/docker/image/import/)。

网页使用主机网络，监听 8800，规避当前飞牛的 Docker 端口代理启动超时。数据库、Redis、Qdrant 和语音内部服务留在独立的 `172.29.61.0/24` 网络内，不向局域网发布数据库端口。
网页通过固定的内部地址访问数据服务。语音网关仍限制原有设备 API 路径与 Bearer 验证，仅将上游端口调整为 8800。

三个命名卷保留原来的名称：`knowledgebase-pgdata`、`knowledgebase-redisdata`、`knowledgebase-qdrantdata-delivery-20261015`。
数据在停止写入和数据库正常关闭后备份、迁移。媒体文件和语音模型单独持久化。
不要挂载旧的 Windows `vector_data/qdrant` 目录。

部署配置引用 `secrets/*.env`，这些文件包含现有配置，只保存在迁移包和飞牛部署目录中，不进入源码或镜像。
首次创建时需要先导入镜像、恢复数据卷，再启动已有容器。
飞牛首次落盘和初始化较慢，使用较长的健康检查启动宽限期，并按基础服务、迁移、网页、后台及语音服务的顺序启动。

Cloudflare 临时语音隧道重启会生成新地址。新地址通过原有机制写入 `media/voice-connection/current.json`，设备可能需要在手机端同步连接地址。
原电脑容器与数据保留，切换成功后停止原电脑服务。
