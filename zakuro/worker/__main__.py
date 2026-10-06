"""``python -m zakuro.worker`` entry point.

Executed only as a script, so it cannot be exercised from an in-process test;
``main()`` itself is covered by tests/test_worker_server.py.
"""

from zakuro.worker.server import main  # pragma: no cover

main()  # pragma: no cover
