"""What a row's confirmation state becomes when copies join it.

The flag says whether every copy in a row has had its condition and variant
stated. Merging is therefore a lattice, not an assignment, and every path that
moves copies into a row has to use the same one - review found four that did
not, each in its own way.

    False  beats everything: one copy nobody assessed makes the row want
           checking, whatever else is in it.
    None   beats True: if either side's history is unknown, the result is
           unknown rather than a claim that everything has been stated.
    True   only when both sides are known to be stated.
"""


def merged_confirmation(current, incoming):
    """Combine the confirmation state of a row and the copies joining it."""
    if current is False or incoming is False:
        return False
    if current is None or incoming is None:
        return None
    return True
