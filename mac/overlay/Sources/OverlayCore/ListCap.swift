public extension Array {
    /// Drops the oldest (leading) elements until at most `limit` remain. `limit <= 0` is a no-op.
    mutating func dropOldest(keeping limit: Int) {
        guard limit > 0, count > limit else { return }
        removeFirst(count - limit)
    }
}
