# Copyright ClusterHQ Inc.  See LICENSE file for details.

"""
A library for implementing state changes on external systems in an isolated
way.

The basic unit of an external system change provided is an ``IStateChange``
provider.

More complex changes can be composed from individual ``IStateChange``
providers.

``run_state_change`` can be used to execute such a complex collection of
changes.
"""

from zope.interface import Interface, Attribute, implementer

from pyrsistent import PVector, pvector, field, PClass

from twisted.internet.defer import maybeDeferred, succeed

from eliot.twisted import DeferredContext
from eliot import ActionType

from ..common import gather_deferreds


class IStateChange(Interface):
    """
    An operation that changes local state.
    """
    eliot_action = Attribute(
        """
        A hack whereby getting this attributes has a side-effect: a
        ``eliot.ActionType`` is started and return. This state change's
        run method should be run within the context of the returned
        action.

        At some point we should fix this so it's a method instead of a
        attribute-which-must-always-be-a-property.
        """)

    def run(deployer):
        """
        Apply the change to local state.

        :param IDeployer deployer: The ``IDeployer`` to use. Specific
            ``IStateChange`` providers may require specific ``IDeployer``
            providers that provide relevant functionality for applying the
            change.

        :return: ``Deferred`` firing when the change is done.
        """

    def __eq__(other):
        """
        Return whether this change is equivalent to another.
        """

    def __ne__(other):
        """
        Return whether this change is not equivalent to another.
        """


def run_state_change(change, deployer):
    """
    Apply the change to local state.

    :param change: Either an ``IStateChange`` provider or the result of an
        ``in_parallel`` or ``sequentially`` call.
    :param IDeployer deployer: The ``IDeployer`` to use.  Specific
        ``IStateChange`` providers may require specific ``IDeployer`` providers
        that provide relevant functionality for applying the change.

    :return: ``Deferred`` firing when the change is done.
    """
    with change.eliot_action.context():
        context = DeferredContext(maybeDeferred(change.run, deployer))
        context.addActionFinish()
        return context.result


LOG_SEQUENTIALLY = ActionType("flocker:node:sequentially", [], [])
LOG_IN_PARALLEL = ActionType("flocker:node:in_parallel", [], [])


@implementer(IStateChange)
class _InParallel(PClass):
    changes = field(
        type=PVector,
        # Sort the changes for the benefit of comparison.  Stick with a vector
        # (rather than, say, a set) in case someone wants to run the same
        # change multiple times in parallel.
        factory=lambda changes: pvector(sorted(changes, key=id)),
        mandatory=True
    )

    @property
    def eliot_action(self):
        return LOG_IN_PARALLEL()

    def run(self, deployer):
        return gather_deferreds(list(
            run_state_change(subchange, deployer)
            for subchange in self.changes
        ))


def in_parallel(changes):
    """
    Run a series of changes in parallel.

    Failures in one change do not prevent other changes from continuing.

    The order in which execution of the changes is started is unspecified.
    Comparison of the resulting object disregards the ordering of the changes.

    @param changes: A sequence of ``IStateChange`` providers.

    @return: ``IStateChange`` provider that will run given changes in
        parallel, or ``NoOp`` instance if changes are empty or all
        ``NoOp``.
    """
    if all(c == NoOp() for c in changes):
        return NoOp()
    return _InParallel(changes=changes)


@implementer(IStateChange)
class _Sequentially(PClass):
    changes = field(type=PVector, factory=pvector, mandatory=True)

    @property
    def eliot_action(self):
        return LOG_SEQUENTIALLY()

    def run(self, deployer):
        d = DeferredContext(succeed(None))
        for subchange in self.changes:
            d.addCallback(
                lambda _, subchange=subchange: run_state_change(
                    subchange, deployer
                )
            )
        return d.result


def sequentially(changes):
    """
    Run a series of changes in sequence, one after the other.

    Failures in earlier changes stop later changes.

    @param changes: A sequence of ``IStateChange`` providers.

    @return: ``IStateChange`` provider that will run given changes
        serially, or ``NoOp`` instance if changes are empty or all
        ``NoOp``.
    """
    if all(c == NoOp() for c in changes):
        return NoOp()
    return _Sequentially(changes=changes)


LOG_NOOP = ActionType("flocker:change:noop", [], [], "We've done nothing.")


@implementer(IStateChange)
class Sleep(PClass):
    """
    Do nothing.
    """
    interval = field(type=timedelta)

    @property
    def eliot_action(self):
        return LOG_NOOP()

    def run(self, deployer):
        return succeed(None)

def get_sleep_interval(state_change):
    if isinstance(state_change, Sleep):
        return state_change.interval
    if isinstance(state_change, _Sequentially, _InParallel):
        return min(map(get_sleep_interval, state_change.changes))
    else:
        return timedelta(seconds=0)

# BlockDevice does:
NOTHING_TO_DO = Sleep(interval=timedelta(seconds=60.0))
WAIT_FOR_REMOTE_DETACH = Sleep(interval=timedelta(seconds=2.0))
