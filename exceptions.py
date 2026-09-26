class SWECycleError(Exception):
    """所有业务逻辑错误的基类"""
    code = "GENERIC_ERROR"  # 业务错误码
    message = "发生了一个错误" # 默认消息

    def __init__(self, message=None, payload=None, **kwargs):
        """
        :param message: 覆盖默认消息
        :param payload: 额外的上下文数据（字典），用于日志记录
        :param kwargs: 用于格式化消息模板的参数
        """
        if message:
            self.message = message
        
        # 如果消息里有 {key} 占位符，尝试用 kwargs 格式化
        # 例如 message="User {uid} missing", kwargs={'uid': 1}
        try:
            if kwargs:
                self.message = self.message.format(**kwargs)
        except KeyError:
            pass # 格式化失败则保持原样，防止异常中的异常

        self.payload = payload or {}
        super().__init__(self.message)

    def to_dict(self):
        return {"code": self.code, "message": self.message}

class genSingleProblemError(SWECycleError):
    # Example Usage: raise genSingleProblemError(docker_id=docker_id, image_name=image_name, tag=tag)
    code = "genSingleProblemError"
    message = "生成{docker_id}到{image_name}:{type}失败"
    
class DockerError(SWECycleError):
    code = "Docker Error"
    message = "Failed to {task} in docker {docker_id}."
    
class DockerNotFoundError(SWECycleError):
    code = "Docker Error"
    message = "Docker {docker_id} doesn't exist."
