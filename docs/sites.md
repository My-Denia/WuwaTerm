# Public WuwaTerm Web

The public workbench now lives in [My-Denia/WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web).
Its browser, same-origin BFF, Node dependencies, tests and hosting build are maintained there.

- [Product and operating guide](https://github.com/My-Denia/WuwaTerm-Web/blob/main/docs/sites.md)
- [Web development and validation](https://github.com/My-Denia/WuwaTerm-Web#development-and-verification)
- [Cross-repository contracts, deployment receipts and rollback order](https://github.com/My-Denia/WuwaTerm-Web/blob/main/docs/cross-repository-delivery.md)
- [Original source-blob mapping](https://github.com/My-Denia/WuwaTerm-Web/blob/main/migration/source.json)

The existing public beta remains at <https://wuwaterm.denia-official.chatgpt.site>.
Repository extraction does not publish a new hosting version or change its resource binding.

This backend repository retains Python core/application logic, HTTP API, Telegram Bot,
dictionary builder, VPS tools, and the private in-process Web. [WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows) owns the desktop product.
The public Web consumes the API; it does not duplicate authoritative translation logic.
One existing Linear WuwaTerm Project coordinates linked changes across both repositories.

The backend contract job checks the exact Web commit in `contracts/web-ref.txt`.
See [Contributing](../CONTRIBUTING.md#api--public-web-contract) for commands.
Strict BFF field allowlists remain: an additive API response change can require a
consumer update. API version, report schema, matcher identity and manuscript format
are separate compatibility boundaries.

Source and CI evidence are not production adoption. Deployment receipts must pair
Web source/build identity with actual API application/image identity and dictionary
hash/provenance. Dictionary source commit is not the API application commit.
Production deployment requires its own current revision/target authorization.
