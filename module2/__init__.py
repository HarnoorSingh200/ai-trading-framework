"""Module 2 - the agent engine.

An agent is a configuration record: a system prompt, one decision timeframe, a
budget and a risk profile. It wakes when a candle of its own timeframe closes,
decides what to look at, screens the universe through Module 1, inspects the two
or three names that survived, and answers with an order or an explicit refusal.

It never reaches the broker, and it never sizes its own position: Module 1 owns
the market, this module owns the reasoning, and the arithmetic between a stop
and a quantity belongs to neither of them.

Layout:

* :mod:`module2.definition` - what an agent is.
* :mod:`module2.store` - the registry, and the two append-only logs.
* :mod:`module2.market` - Module 1's HTTP surface, as the tools see it.
* :mod:`module2.llm` - the model provider, behind one narrow protocol.
* :mod:`module2.tools` - the tools a model may call, and their budget.
* :mod:`module2.decision` - validation and position sizing.
* :mod:`module2.worker` - one agent's loop, in its own process.
* :mod:`module2.service` - the control surface the operator site talks to.
"""

from module2.config import Settings, load_settings

__all__ = ["Settings", "__version__", "load_settings"]

__version__ = "0.1.0"
