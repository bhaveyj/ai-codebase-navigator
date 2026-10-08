# Beacon Store — analysis fixture

This small, fictional storefront is bundled to demonstrate actual static analysis.
It is source evidence, not a live checkout service. The navigator parses these
files with the same analyzer used for public GitHub repositories.

The Express server mounts authentication and checkout routes. The login route
looks up a user and creates a session. Authenticated checkout validates the cart,
checks inventory, creates a payment, and stores an order. The React client sends
requests through a shared API client. Middleware rate-limits requests by IP.

External packages are deliberately not installed for repository analysis.

