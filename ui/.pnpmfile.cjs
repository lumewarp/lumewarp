// openapi-typescript drives the TypeScript compiler JS API, which TypeScript 7 (native) lacks.
// Give it its own TypeScript 5 while the app itself type-checks with 7.
module.exports = {
  hooks: {
    readPackage(pkg) {
      if (pkg.name === "openapi-typescript") {
        delete pkg.peerDependencies?.typescript;
        pkg.dependencies = { ...pkg.dependencies, typescript: "5.9.3" };
      }
      return pkg;
    },
  },
};
