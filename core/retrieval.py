"""Fact and document retrieval, in both deployment flavours.

The prod profile searches by cosine distance over pgvector; the small profile
has no embeddings service at all and searches with PostgreSQL full text search
instead (``plan/small-profile.md`` section 4.1). The full text queries live here
so that the difference between the two backends is readable in one place, and
the backend arrives as an argument rather than being read from the environment,
so each branch is testable on its own.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
from asyncpg.pool import PoolConnectionProxy

# A whole user message is the query when facts are injected automatically, so
# the lexemes have to be OR-ed: websearch_to_tsquery and plainto_tsquery both
# AND their terms, and the conjunction of every content word in a sentence
# matches no short fact at all. The lexemes come out of to_tsvector already
# normalised, so casting them straight to tsquery avoids a second stemming pass.
# All stop words (or empty input) yield NULL, which matches nothing -- correct.
_OR_TSQUERY = (
    "(SELECT string_agg(quote_literal(lexeme), ' | ') "
    "FROM unnest(to_tsvector('russian', {argument})))::tsquery"
)
_AND_TSQUERY = "websearch_to_tsquery('russian', {argument})"

FACT_TRIGRAM_DUPLICATE_THRESHOLD = 0.7
"""Above this, two facts are the same fact written twice.

Measured on Russian pairs against the dev database (2026-09-07): repeats,
typos, truncations and word reorderings scored 0.667-1.000, while genuinely
different facts scored 0.042-0.565. The threshold sits at the top of that gap on
purpose -- a false positive silently discards a new fact, while a false negative
only stores a near-duplicate, which the plan already accepts as cosmetic.
"""


async def search_facts_fts(
    connection: PoolConnectionProxy[asyncpg.Record],
    text: str,
    limit: int,
) -> list[asyncpg.Record]:
    """Rank the user's facts against a whole message by full text search.

    Relevance is gated by the match itself: ``ts_rank`` is not normalised and is
    not comparable across queries, so there is no analogue of the cosine
    threshold the vector branch uses. Recency breaks ties, which on facts this
    short is most of the time.
    """

    query = _OR_TSQUERY.format(argument="$1")
    return await connection.fetch(
        f"""
        SELECT id, text
        FROM memory_facts
        WHERE text_tsv @@ {query}
        ORDER BY ts_rank(text_tsv, {query}) DESC, last_used_at DESC
        LIMIT $2
        """,
        text,
        limit,
    )


async def find_duplicate_fact_fts(
    connection: PoolConnectionProxy[asyncpg.Record],
    fact: str,
) -> UUID | None:
    """Return the id of an existing fact that says the same thing, if any.

    Trigram similarity, not full text search: two different facts about one
    topic ("allergic to penicillin", "allergic to cats") match each other under
    full text search without being duplicates at all.
    """

    duplicate = await connection.fetchval(
        """
        SELECT id
        FROM memory_facts
        WHERE similarity(text, $1) >= $2
        ORDER BY similarity(text, $1) DESC
        LIMIT 1
        """,
        fact,
        FACT_TRIGRAM_DUPLICATE_THRESHOLD,
    )
    if duplicate is None:
        return None
    if not isinstance(duplicate, UUID):
        raise TypeError("memory_facts.id must be a uuid")
    return duplicate


async def search_chunks_fts(
    connection: PoolConnectionProxy[asyncpg.Record],
    query: str,
    doc_id: int | None,
    limit: int,
) -> list[asyncpg.Record]:
    """Search ready documents, widening the query only when it finds nothing.

    The model writes this query, so it is short and deliberate and the implicit
    AND of ``websearch_to_tsquery`` is the right first attempt. Trigram
    similarity is deliberately not the fallback: it normalises over the trigrams
    of both strings, so a short query against an 800-token chunk scores near
    zero whether or not it is relevant.
    """

    strict = await _fetch_chunks(connection, _AND_TSQUERY, query, doc_id, limit)
    if strict:
        return strict
    return await _fetch_chunks(connection, _OR_TSQUERY, query, doc_id, limit)


async def _fetch_chunks(
    connection: PoolConnectionProxy[asyncpg.Record],
    tsquery_template: str,
    query: str,
    doc_id: int | None,
    limit: int,
) -> list[asyncpg.Record]:
    tsquery = tsquery_template.format(argument="$1")
    return await connection.fetch(
        f"""
        SELECT c.doc_id, d.filename, c.page, c.text
        FROM doc_chunks AS c
        JOIN documents AS d ON d.id = c.doc_id
        WHERE d.status = 'ready'
          AND ($2::bigint IS NULL OR c.doc_id = $2)
          AND c.text_tsv @@ {tsquery}
        ORDER BY ts_rank(c.text_tsv, {tsquery}) DESC
        LIMIT $3
        """,
        query,
        doc_id,
        limit,
    )
