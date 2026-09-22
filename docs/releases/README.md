# Release notes

One file per tag, named after it: `v0.1.0.md` holds the notes for `v0.1.0`.

`.github/workflows/release.yml` turns the file into a **draft** GitHub release when the tag is
pushed. Publishing stays manual: publishing is the moment HACS tells every installation an
update exists.

Write the notes file and commit it **before** cutting the tag, so it rides in the tag's own
tree. The workflow falls back to the default branch if you forget, but then the tag describes a
commit that does not contain its own notes.
