"""The framework's own supervisor: start it in order, watch it, stop it on time.

`python -m runner` is what a cloud machine runs. It is not a fifth trading
module - it holds no position and no market data - it is the thing that turns
four services into one shift:

1. start Module 1 and wait until the broker session, the history and the feed
   are actually up (at least a minute, so a fast answer is not mistaken for a
   stable one);
2. start Module 3 and Module 4, then Module 2, whose control service starts
   every defined agent by itself;
3. watch: a service that dies is restarted and reported, and a health check
   that starts failing is reported once per change;
4. stop on the clock the operator set - agents when the decision window
   closes, everything when the framework closes.
"""
