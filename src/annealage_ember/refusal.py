"""The one distinction the turn path makes between a refusal and a delay."""


class Busy(str):
    """A reason a message cannot go in right now but will be able to: work in
    progress, a full queue, a session being replaced. A message refused for a
    ``Busy`` reason is kept and sent in order when it clears (``http.ws``). Any
    other reason is a fault, and the page is told. Everything that gates a
    turn returns ``Busy`` for a passing state, so no caller matches on text."""
