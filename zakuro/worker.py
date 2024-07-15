
class Worker:
    def __init__(self, worker):
        from zakuro import ctx
        assert worker in ctx.workers
        self._worker = worker
        
    def submit(self, f, *args, **kwargs):
        from zakuro import ctx
        return ctx.submit(f, *args, **kwargs, workers=self._worker, allow_other_workers=False, pure=False)