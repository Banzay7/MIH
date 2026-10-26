-- Programme search (spec 20). Text is folded in Python (case, ё/е, diacritics outside
-- Cyrillic) before it is indexed, so the tokenizer must not strip diacritics itself:
-- that would also turn й into и. Rows share rowid with programmes; mih.search.index
-- keeps them in step and rebuilds them when the counts disagree.
CREATE VIRTUAL TABLE programme_fts USING fts5(
    title, sub_title, description, categories,
    tokenize = "unicode61 remove_diacritics 0"
);

-- Substrings of titles, for queries such as "здоров" that are not a word prefix.
CREATE VIRTUAL TABLE programme_trigram USING fts5(title, tokenize = "trigram");
