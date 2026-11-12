def _normalize_value(value):
    if isinstance(value, str):
        return value
    elif isinstance(value, bool):
        return "" if value else None
    elif hasattr(value, "__iter__"):
        return "|".join(str(v) for v in value)
    else:
        return value


def _merge_values(old, new):
    """
    Recursively merges two values decoded from JSON response documents.
    Dictionaries are merged key-by-key, lists are concatenated in order
    and any other (scalar) value is replaced by the newer one.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        merged = dict(old)
        for key, value in new.items():
            if key in merged:
                merged[key] = _merge_values(merged[key], value)
            else:
                merged[key] = value
        return merged
    elif isinstance(old, list) and isinstance(new, list):
        return old + new
    else:
        return new


def _extract_pages(doc):
    """
    Extracts the list of pages from a query response document.  Handles
    both formatversion=1 (``query.pages`` is a dict keyed by page ID) and
    formatversion=2 (``query.pages`` is a list).  Returns an empty list
    when the document contains no pages.
    """
    query = doc.get('query') or {}
    pages = query.get('pages')
    if pages is None:
        return []
    if isinstance(pages, dict):
        # formatversion=1: pages is a dict keyed by page ID.  Values
        # keep their first-appearance (insertion) order.
        return list(pages.values())
    # formatversion=2: pages is already a list
    return list(pages)


def _page_key(page):
    """
    Returns the merge key for a page: its ``pageid`` when present,
    otherwise its ``title`` (e.g. for missing pages without an ID).
    """
    if 'pageid' in page:
        return ('pageid', page['pageid'])
    return ('title', page.get('title'))


def _merge_page(batch, index, page):
    """
    Merges a page into a batch.  ``batch`` is the ordered list of pages
    (sorted by first appearance) and ``index`` maps merge keys to
    positions in that list.  Pages with a ``pageid`` are merged by ID,
    pages without an ID are merged by title.
    """
    key = _page_key(page)
    if key in index:
        position = index[key]
        batch[position] = _merge_values(batch[position], page)
    else:
        index[key] = len(batch)
        batch.append(page)


def _normalize_params(params, query_continue=None):
    normal_params = {k: _normalize_value(v) for k, v in params.items()}
    normal_params = {k: v for k, v in normal_params.items() if v is not None}

    if query_continue is not None:
        normal_params.update(query_continue)

    return normal_params
