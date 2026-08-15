# Present only so `python -m unittest discover` can reach reconcile/tests:
# discovery has refused implicit namespace packages since Python 3.11, and a
# discover that reaches nothing is a test gate that can never fail.
