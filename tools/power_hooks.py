# Placeholder for energy measurements; intentionally empty to avoid fake data.
class PowerHook:
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def sample(self): return {}
