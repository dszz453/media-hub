"""容器入口：先以 root 修好数据卷权限，再降权到 appuser 启动服务。

为什么需要这一步？
    compose 里 /data 是绑定挂载（./data:/data）。如果宿主机的 ./data
    是 Docker 或 root 创建的，属主就是 root:root；而服务是以 uid 1000
    运行的，于是启动时 mkdir /data/cache 会直接 PermissionError 崩掉。

    在 Dockerfile 里 useradd + chown 是没用的 —— bind mount 会盖掉镜像里
    的属主信息。所以只能在启动时补一次 chown，然后 setuid 降权再 exec。

只在属主不对时才递归修（正常情况下只做一次 stat，开销可忽略）。
"""

from __future__ import annotations

import os
import pwd
import sys

TARGET_UID = 1000
TARGET_GID = 1000


def _fix_ownership(path: str) -> None:
    try:
        if os.stat(path).st_uid == TARGET_UID:
            return  # 属主已经对了，不做无谓的递归
        os.chown(path, TARGET_UID, TARGET_GID)
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    os.chown(os.path.join(root, name), TARGET_UID, TARGET_GID)
                except OSError:
                    pass  # 个别文件修不动就算了，不影响启动
    except OSError as exc:
        print(f"[entrypoint] 修正 {path} 属主失败：{exc}", file=sys.stderr)


def _drop_privileges() -> None:
    try:
        info = pwd.getpwuid(TARGET_UID)
    except KeyError:
        print(f"[entrypoint] 找不到 uid={TARGET_UID} 的用户，保持当前身份运行", file=sys.stderr)
        return
    try:
        os.initgroups(info.pw_name, info.pw_gid)
        os.setgid(info.pw_gid)
        os.setuid(info.pw_uid)
        print(f"[entrypoint] 已降权到 {info.pw_name}(uid={info.pw_uid})")
    except OSError as exc:
        print(f"[entrypoint] 降权失败，继续以 root 运行：{exc}", file=sys.stderr)


def main() -> None:
    if os.geteuid() == 0:
        data_dir = os.environ.get("DATA_DIR", "/data")
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError as exc:
            print(f"[entrypoint] 创建 {data_dir} 失败：{exc}", file=sys.stderr)
        else:
            _fix_ownership(data_dir)
        _drop_privileges()

    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
