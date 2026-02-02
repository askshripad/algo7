from nifty_algo.strategies.strangle_0915 import Strangle0915Strategy

STRATEGIES = {
    "strangle_0915": Strangle0915Strategy,
}


def get_strategy(name: str):
    key = name.lower().strip()
    if key in STRATEGIES:
        return STRATEGIES[key]()
    raise ValueError(f"Unknown strategy: {name}")