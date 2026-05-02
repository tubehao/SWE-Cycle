import logging
import logging.config
import logging.handlers
import os
import sys
import threading
import copy
from queue import Queue

# Thread-local storage for worker ID (用于在多线程环境中传递 worker 标识)
_worker_id_storage = threading.local()

# ==========================================
# 1. 新增：自定义颜色 Formatter 类
# ==========================================
class ColorFormatter(logging.Formatter):
    """
    自定义日志格式化器，根据日志级别添加颜色
    """
    # ANSI 颜色代码
    GREY = "\x1b[38;20m"
    GREEN = "\x1b[32;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    RESET = "\x1b[0m"

    def __init__(self, fmt=None, datefmt=None, style='%', validate=True):
        super().__init__(fmt, datefmt, style, validate)
        # 定义不同级别的格式（将颜色代码包裹在 format 字符串周围）
        # 注意：这里我们假设 fmt 参数就是用户传入的基础格式字符串
        self.FORMATS = {
            logging.DEBUG: self.GREY + fmt + self.RESET,
            # logging.INFO: self.GREEN + fmt + self.RESET,
            logging.WARNING: self.YELLOW + fmt + self.RESET,
            logging.ERROR: self.YELLOW + fmt + self.RESET,
            logging.CRITICAL: self.BOLD_RED + fmt + self.RESET
        }

    def format(self, record):
        # 保存原始的 format，因为 logging 内部是单例模式，不保存会污染其他 handler
        original_fmt = self._style._fmt
        
        # 根据级别选择带颜色的格式
        log_fmt = self.FORMATS.get(record.levelno, original_fmt)
        
        # 临时修改当前 formatter 的格式
        self._style._fmt = log_fmt
        
        # 执行格式化
        result = super().format(record)
        
        # 还原格式（非常重要，否则会影响后续日志或文件日志）
        self._style._fmt = original_fmt
        return result


class WorkerIDFilter(logging.Filter):
    """自定义 Filter，为日志记录添加 worker ID"""
    def filter(self, record):
        worker_id = getattr(_worker_id_storage, 'worker_id', '')
        if worker_id:
            # 在日志消息前添加 worker 标识
            record.msg = f"[{worker_id}] {record.msg}"
        return True

# ==========================================
# 2. 修改 setup_logging 函数，支持线程安全的日志
# ==========================================
# 全局队列和监听器（用于线程安全的日志）
_log_queue = None
_log_listener = None

def setup_logging(
    level=logging.INFO,
    log_file='tmplog.log', 
    to_console=True,       
    to_file=True, 
    file_level=logging.DEBUG,
    console_formatter_name='colored', # 修改默认值为 'colored'
    use_queue=True  # 新增：是否使用队列处理（多线程时推荐）
):
    global _log_queue, _log_listener
    
    active_handlers = []
    if to_console:
        active_handlers.append('console')
    if to_file:
        active_handlers.append('file')
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)

    if not active_handlers:
        active_handlers = ['console']
        to_console = True 

    # 基础格式字符串
    STANDARD_FORMAT = '%(asctime)s [%(levelname)s] %(name)s, %(filename)s:%(funcName)s:%(lineno)d: %(message)s'
    SIMPLE_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'

    # 如果使用队列，先创建队列和实际处理器
    if use_queue:
        _log_queue = Queue(-1)  # 无界队列
        
        # 创建实际的处理器（这些会在单独的线程中运行）
        real_handlers = []
        if to_console:
            console_handler = logging.StreamHandler(sys.stdout)
            if console_formatter_name == 'colored':
                console_handler.setFormatter(ColorFormatter(STANDARD_FORMAT))
            else:
                console_handler.setFormatter(logging.Formatter(STANDARD_FORMAT))
            console_handler.setLevel(level)
            real_handlers.append(console_handler)
        
        if to_file:
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
            )
            file_handler.setFormatter(logging.Formatter(STANDARD_FORMAT))
            file_handler.setLevel(file_level)
            real_handlers.append(file_handler)
        
        # 创建队列监听器（在单独线程中处理日志）
        _log_listener = logging.handlers.QueueListener(_log_queue, *real_handlers, respect_handler_level=True)
        _log_listener.start()
        
        # 配置使用 QueueHandler
        LOGGING_CONFIG = {
            'version': 1,
            'disable_existing_loggers': False,
            'formatters': {
                'standard': {
                    'format': STANDARD_FORMAT
                },
                'simple': { 
                    'format': SIMPLE_FORMAT
                },
                'colored': {
                    '()': ColorFormatter, 
                    'fmt': STANDARD_FORMAT
                }
            },
            'filters': {
                'worker_id': {
                    '()': WorkerIDFilter,
                }
            },
            'handlers': {
                'queue': {
                    'class': 'logging.handlers.QueueHandler',
                    'queue': _log_queue,
                    'filters': ['worker_id'],  # 添加 worker_id filter
                },
            },
            'loggers': {
                '': { 
                    'handlers': ['queue'],
                    'level': level,
                    'propagate': False
                }
            }
        }
    else:
        # 不使用队列，直接配置（单线程模式）
        LOGGING_CONFIG = {
            'version': 1,
            'disable_existing_loggers': False,
            'formatters': {
                'standard': {
                    'format': STANDARD_FORMAT
                },
                'simple': { 
                    'format': SIMPLE_FORMAT
                },
                'colored': {
                    '()': ColorFormatter, 
                    'fmt': STANDARD_FORMAT
                }
            },
            'filters': {
                'worker_id': {
                    '()': WorkerIDFilter,
                }
            },
            'handlers': {
                'console': {
                    'level': level,
                    'class': 'logging.StreamHandler',
                    'formatter': console_formatter_name,
                    'stream': 'ext://sys.stdout',
                    'filters': ['worker_id'],  # 添加 worker_id filter
                },
                'file': {
                    'level': file_level,
                    'class': 'logging.handlers.RotatingFileHandler',
                    'filename': log_file,
                    'maxBytes': 10*1024*1024, 
                    'backupCount': 5,
                    'formatter': 'standard',
                    'encoding': 'utf-8',
                    'filters': ['worker_id'],  # 添加 worker_id filter
                },
            },
            'loggers': {
                '': { 
                    'handlers': active_handlers,
                    'level': level,
                    'propagate': True
                }
            }
        }

    logging.config.dictConfig(LOGGING_CONFIG)
    
    logger = logging.getLogger()

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        
        logger.error("Uncaught Exception:", exc_info=(exc_type, exc_value, exc_traceback))
        
        if not to_console:
             sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = handle_exception

    if hasattr(threading, 'excepthook'):
        def handle_thread_exception(args):
            logger.error(f"Uncaught Exception in thread {args.thread.name}:", 
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.excepthook = handle_thread_exception
    
    # 测试不同级别的颜色
    # logger.debug("This is a debug message (Grey)")
    # logger.info("Logging setup complete (Green)")
    # logger.warning("This is a warning (Yellow)")
    # logger.error("This is an error (Red)")
    # logger.critical("This is critical (Bold Red)")

def stop_logging():
    """停止日志监听器（在程序退出时调用）"""
    global _log_listener
    if _log_listener is not None:
        _log_listener.stop()
        _log_listener = None


def set_worker_id(worker_id: str):
    """设置当前线程的 worker ID（用于多线程环境）"""
    _worker_id_storage.worker_id = worker_id


def get_worker_id() -> str:
    """获取当前线程的 worker ID"""
    return getattr(_worker_id_storage, 'worker_id', '')

if __name__ == "__main__":
    # 确保 level 足够低以显示 debug 信息
    setup_logging(to_console=True, to_file=True, level=logging.DEBUG)
    
    # 测试崩溃
    # raise RuntimeError("Test Crash")