#!/usr/bin/env python
"""便捷启动脚本"""

import sys

from nano_search_mcp.server import main as _server_main


def main() -> None:
    """CLI 入口，显式转发命令行参数。"""
    _server_main(sys.argv[1:])

if __name__ == "__main__":
    main()
