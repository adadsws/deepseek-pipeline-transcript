# VideoCaptioner 上游源代码

- 用途：提供本项目实际运行的 VideoCaptioner GUI/CLI 源代码。
- 上游仓库：https://github.com/WEIFENG2333/VideoCaptioner
- 固定版本：`v1.4.2`
- 完整提交：`d753521d57cf2311df96bfee96fe39c6306a8e09`
- 存放位置：`reference/upstream/VideoCaptioner/`
- 使用方式：项目 `.venv` 以 editable 方式安装该目录；根目录 `start.bat` 负责启动 GUI。
- 隔离要求：该目录保持 detached HEAD、工作树干净；项目自有启动配置统一放在根目录 `config/`。
- 重建方式：

  ```powershell
  git clone --branch v1.4.2 --depth 1 --single-branch https://github.com/WEIFENG2333/VideoCaptioner.git reference/upstream/VideoCaptioner
  ```
