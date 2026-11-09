// Print LLVM's native CFG records without computing branch frequencies.
// LLVM_COMPARE_CFG_DIR must name an existing output directory.
#include "llvm/Analysis/CFGPrinter.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/GraphWriter.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"
#include <cstdlib>

using namespace llvm;

namespace {
class PlainCFGPass : public PassInfoMixin<PlainCFGPass> {
public:
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
    const char *Directory = std::getenv("LLVM_COMPARE_CFG_DIR");
    if (!Directory)
      report_fatal_error("LLVM_COMPARE_CFG_DIR is required");
    // Printing an unnamed block operand builds a module slot tracker. Avoid
    // rebuilding it per block in modules with large debug metadata. Do this
    // here because the ordinary instnamer pass skips optnone functions.
    unsigned Block = 0;
    for (BasicBlock &BB : F) {
      if (!BB.hasName())
        BB.setName("compare.bb." + std::to_string(Block));
      ++Block;
    }
    // The function symbol is in the DOT title; ordinal filenames also support
    // mangled symbols longer than the filesystem's filename limit.
    static unsigned Serial = 0;
    SmallString<256> Path(Directory);
    sys::path::append(Path, "cfg." + std::to_string(Serial++) + ".dot");
    std::error_code Error;
    raw_fd_ostream Stream(Path, Error);
    if (Error)
      report_fatal_error(Twine("Cannot write CFG: ") + Error.message());
    DOTFuncInfo Info(&F);
    WriteGraph(Stream, &Info, /*ShortNames=*/true);
    return PreservedAnalyses::all();
  }
  static bool isRequired() { return true; }
};
} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "plain-dot-cfg", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &PM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (Name != "plain-dot-cfg")
                    return false;
                  PM.addPass(PlainCFGPass());
                  return true;
                });
          }};
}
