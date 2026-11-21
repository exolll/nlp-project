import argparse
from pathlib import Path

import uvicorn
from app.core.settings import Settings
from app.main import create_app


def main():
    parser = argparse.ArgumentParser(description='开发环境：真实本地文件持久化与隔离解析；无需数据库，不验证S3连接。')
    parser.add_argument('--test-data-directory', type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(environment='development', storage_backend='local',
                        local_storage_directory=args.test_data_directory.resolve() / 'objects')
    print('DEVELOPMENT ONLY: private local files and isolated parser; no database; S3 not verified.')
    uvicorn.run(create_app(settings=settings), host='127.0.0.1', port=8000)


if __name__ == '__main__':
    main()
