//===----------------------------------------------------------------------===//
// Copyright (c) 2026, Modular Inc. All rights reserved.
//
// Licensed under the Apache License v2.0 with LLVM Exceptions:
// https://llvm.org/LICENSE.txt
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//===----------------------------------------------------------------------===//

#include "Pipeline.h"
#include "Mojo/KGENDialect/KGENOps.h"
#include "Mojo/ToolCommon/KGENPasses.h"
#include "Support/DebugInfoDialect/DebugInfoToLLVM/DebugInfoToLLVM.h"
#include "Support/DebugInfoDialect/Transforms/Passes.h"
#include "mlir/Dialect/LLVMIR/LLVMDialect.h"
#include <set>
#include <functional>
#include "llvm/Support/raw_ostream.h"
#include "mlir/Pass/Pass.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/PassManager.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"
#include "mlir/Transforms/Passes.h"

using namespace M;

namespace {
/// Writes, for the manifest of the split cache, the declarations that each
/// declaration of the program names in its types and attributes, while the
/// program still has the types of the source. Later stages replace a struct
/// by its layout, and nothing then tells which functions a changed field
/// reaches.
///
/// One line per declaration that names another one: the operation, the
/// qualified name, "FILE:LINE", then the names, separated by tabs. Names of
/// the standard library are left out.
struct RecordDeclarationUses
    : public mlir::PassWrapper<RecordDeclarationUses,
                               mlir::OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RecordDeclarationUses)

  explicit RecordDeclarationUses(std::string path) : path(std::move(path)) {}
  llvm::StringRef getName() const override { return "RecordDeclarationUses"; }
  std::unique_ptr<mlir::Pass> clonePass() const override {
    return std::make_unique<RecordDeclarationUses>(path);
  }

  /// The symbol paths in the printed form of a type or attribute:
  /// `@a::@b::@"c d"` gives "a::b::c d".
  static void scan(llvm::StringRef text, std::vector<std::string> &names) {
    for (size_t i = 0, e = text.size(); i < e; ++i) {
      if (text[i] != '@')
        continue;
      std::string name;
      while (i < e && text[i] == '@') {
        size_t begin = ++i;
        if (i < e && text[i] == '"') {
          begin = ++i;
          while (i < e && text[i] != '"')
            i += text[i] == '\\' ? 2 : 1;
          name += text.substr(begin, std::min(i, e) - begin);
          ++i;
        } else {
          while (i < e && (isalnum(text[i]) || text[i] == '_' ||
                           text[i] == '$' || text[i] == '.'))
            ++i;
          name += text.substr(begin, i - begin);
        }
        if (i + 2 < e && text[i] == ':' && text[i + 1] == ':' &&
            text[i + 2] == '@') {
          name += "::";
          i += 2;
        } else {
          break;
        }
      }
      if (!name.empty() && !llvm::StringRef(name).starts_with("std::"))
        names.push_back(std::move(name));
    }
  }

  void runOnOperation() override {
    std::error_code ec;
    llvm::raw_fd_ostream os(path, ec);
    if (ec)
      return;
    // The names in each distinct type and attribute, found once.
    llvm::DenseMap<const void *, std::vector<std::string>> cache;
    auto namesIn = [&](const void *key, auto print)
        -> const std::vector<std::string> & {
      auto [it, isNew] = cache.try_emplace(key);
      if (isNew) {
        std::string text;
        llvm::raw_string_ostream stream(text);
        print(stream);
        scan(text, it->second);
      }
      return it->second;
    };
    std::function<void(mlir::Operation &, std::string, std::set<std::string> *)>
        visit = [&](mlir::Operation &op, std::string scope,
                    std::set<std::string> *uses) {
          std::set<std::string> own;
          auto nameAttr = op.getAttrOfType<mlir::StringAttr>("sym_name");
          bool declaration = nameAttr && op.getNumRegions() != 0;
          if (declaration) {
            scope += (scope.empty() ? "" : "::") + nameAttr.getValue().str();
            uses = &own;
          }
          if (uses) {
            auto add = [&](const std::vector<std::string> &names) {
              uses->insert(names.begin(), names.end());
            };
            for (mlir::Type type : op.getResultTypes())
              add(namesIn(type.getAsOpaquePointer(),
                          [&](llvm::raw_ostream &o) { type.print(o); }));
            for (mlir::NamedAttribute attr : op.getAttrs())
              if (attr.getName() != "sym_name" &&
                  !llvm::isa<mlir::StringAttr>(attr.getValue()))
                add(namesIn(attr.getValue().getAsOpaquePointer(),
                            [&](llvm::raw_ostream &o) {
                              attr.getValue().print(o);
                            }));
            for (mlir::Region &region : op.getRegions())
              for (mlir::Block &block : region)
                for (mlir::BlockArgument arg : block.getArguments())
                  add(namesIn(arg.getType().getAsOpaquePointer(),
                              [&](llvm::raw_ostream &o) {
                                arg.getType().print(o);
                              }));
          }
          for (mlir::Region &region : op.getRegions())
            for (mlir::Block &block : region)
              for (mlir::Operation &nested : block)
                visit(nested, scope, uses);
          // A function that names nothing is of no use to the reader; a
          // type is listed in any case, so that it can be found.
          if (!declaration ||
              (own.empty() && op.getName().getStringRef() == "lit.fn"))
            return;
          std::string where;
          mlir::LocationAttr(op.getLoc()).walk([&](mlir::Location nested) {
            auto file = llvm::dyn_cast<mlir::FileLineColLoc>(nested);
            if (!file)
              return mlir::WalkResult::advance();
            where = (file.getFilename().getValue() + ":" +
                     llvm::Twine(file.getLine()))
                        .str();
            return mlir::WalkResult::interrupt();
          });
          os << op.getName().getStringRef() << '\t' << scope << '\t' << where;
          for (const std::string &name : own)
            os << '\t' << name;
          os << '\n';
        };
    for (mlir::Operation &op : *getOperation().getBody())
      visit(op, "", nullptr);
  }

  std::string path;
};
} // namespace
using namespace KGEN;

void KGEN::buildCheckLITPipeline(mlir::PassManager &pm,
                                 const CompilationOptions &options) {
  // Lower semantic control flow operations like lit.return to terminators and
  // diagnose unreachable code.
  pm.addPass(createLowerSemanticCF());
#ifndef MODULAR_PRODUCTION
  pm.addPass(createVerifyParameters());
#endif

  // These passes doesn't touch parameters, no need to re-verify them after it.
  // Insert calls to destructors, reject use before free, and borrow check.
  pm.addPass(createCheckLifetimes(
      {/*extendTrivialDebugLifetimes=*/options.optimizationLevel == 0}));
}

/// This populates the early pipeline passes of the KGEN compiler that lowers
/// the lit dialect.
static void buildLowerLITPipeline(mlir::PassManager &pm,
                                  const CompilationOptions &options) {
  buildCheckLITPipeline(pm, options);

  if (!options.splitCacheManifest.empty())
    pm.addPass(std::make_unique<RecordDeclarationUses>(
        options.splitCacheManifest + ".use"));

  pm.addPass(createLowerLIT(
      {static_cast<llvm::dwarf::SourceLanguage>(options.debugInfoLanguage)}));
#ifndef MODULAR_PRODUCTION
  pm.addPass(createVerifyParameters());
#endif
}

void KGEN::buildGenerateLibraryPipeline(mlir::PassManager &pm,
                                        const CompilationOptions &options) {
  // If the compilation options aren't for full debug, strip the extra info
  // from the module.
  if (options.debugLevel != CompilationOptions::kFullDebugInfo) {
    pm.addPass(DebugInfo::createDebugInfoStrip(
        {/*preserveLineTables=*/options.debugLevel ==
         CompilationOptions::kLineTablesOnly}));
  }

  buildLowerLITPipeline(pm, options);

  if (options.optimizationLevel >= 2)
    pm.addPass(createRemoveUnusedParams());

  // Eliminate dead symbols. If we don't use the symbol *somewhere* it doesn't
  // need to be in the IR.
  pm.addPass(createEliminateDeadSymbols());

  if (options.optimizationLevel >= 1) {
    pm.addNestedPass<GeneratorOp>(createSROA());
    pm.addNestedPass<GeneratorOp>(createMem2Reg());
    pm.addNestedPass<GeneratorOp>(createCanonicalizer());
  }

  // Only inline `always_inline_no_debug` functions during parametric inlining.
  // Too much inlining pre-elaboration increases pressure on the elaborator and
  // reduces cache granularity. By restricting inlining to `nodebug` functions,
  // we still maintain the zero-cost abstraction.
  InlineParametricOptions inlinerOpts;
  inlinerOpts.nodebugOnly = true;
  inlinerOpts.optimizationLevel = options.optimizationLevel;
  inlinerOpts.updateDebugInfo =
      options.debugLevel != CompilationOptions::DebugInfoLevel::kNoDebug;
  // FIXME(#32286): The bodies of precompiled functions are not available to the
  // parametric inliner.
  pm.addPass(createInlineParametric(inlinerOpts));
  pm.addPass(createVerifyParameters({/*simplifyParameters=*/true}));

  // These passes don't influence parameters, so we don't need to verify them.
  if (options.optimizationLevel >= 1) {
    if (options.optimizationLevel >= 2)
      pm.addPass(createRemoveUnusedParams());
    pm.addPass(createEliminateDeadSymbols());
    pm.addNestedPass<GeneratorOp>(createSROA());
    pm.addNestedPass<GeneratorOp>(createMem2Reg());
    pm.addNestedPass<GeneratorOp>(createSCCP());
    pm.addNestedPass<GeneratorOp>(createCanonicalizer());
    if (options.optimizationLevel >= 2)
      pm.addPass(createRemoveUnusedParams());
    pm.addPass(createEliminateDeadSymbols());
    pm.addPass(createApplyInliner());
    pm.addPass(createInlineParametric(inlinerOpts));
    pm.addPass(createVerifyParameters({/*simplifyParameters=*/true}));
    pm.addNestedPass<GeneratorOp>(createSROA());
    pm.addNestedPass<GeneratorOp>(createMem2Reg());
    pm.addNestedPass<GeneratorOp>(createCanonicalizer());
    if (options.optimizationLevel >= 2)
      pm.addPass(createRemoveUnusedParams());
  }
}

void KGEN::buildElaborateModulePipeline(
    mlir::PassManager &pm, TargetInfoAttr target,
    const CompilationOptions &options, ElaboratorCompileAsmFn compileAsmFn,
    ElaboratorCompileOffloadFn compileOffloadFn) {
  pm.addPass(createEliminateDeadSymbols());

  // Erase debuginfo from all sources if compiling with no debuginfo.
  if (options.debugLevel == CompilationOptions::kNoDebug)
    pm.addPass(DebugInfo::createDebugInfoStrip());

  // Only outline closures just before elaboration - they aren't really
  // necessary until elaboration happens.
  pm.addPass(createOutlineClosures(OutlineClosuresOptions{
      options.debugLevel != CompilationOptions::kNoDebug}));
  if (options.optimizationLevel >= 1) {
    // TODO(#20717): CSE cannot run before `OutlineClosures`.
    pm.addNestedPass<GeneratorOp>(mlir::createCSEPass());
  }
#ifndef MODULAR_PRODUCTION
  pm.addPass(createVerifyParameters());
#endif
  pm.addPass(createLiftAndFoldApply());

  pm.addPass(createReorderParamOps(true));

  // After elaboration, we have no use for the parameter verifier anymore.
  ElaborateGeneratorsOptions elaboratorOptions;
  elaboratorOptions.elaborateDebugInfo =
      options.debugLevel == CompilationOptions::kLineTablesOnly ||
      options.debugLevel == CompilationOptions::kFullDebugInfo;
  // When compiling with -O0, optimize functions before interpreting them.
  elaboratorOptions.optimizeInterpreter = options.optimizationLevel == 0;
  elaboratorOptions.useParametricInterpreter = options.useParametricInterpreter;
  elaboratorOptions.errorIncludePrelude =
      options.elaborationErrorIncludePrelude;
  elaboratorOptions.errorVerbose = options.elaborationErrorVerbose;
  elaboratorOptions.maxDepth = options.elaborationMaxDepth;
  elaboratorOptions.loopUnrollingWarnThreshold =
      options.loopUnrollingWarnThreshold;
  pm.addPass(createElaborateGenerators(target, elaboratorOptions, options,
                                       compileAsmFn, compileOffloadFn));
}

void KGEN::buildPostElaborationPipeline(mlir::PassManager &pm,
                                        const CompilationOptions &options) {
  // Apply the `-fp-mode` fast-math policy on the freshly-elaborated POP ops,
  // before any lowering reads their flags.
  pm.addPass(
      createSetFastMathFlags(SetFastMathFlagsOptions{options.fpMode.contract}));

  // Verify elaborated IR; target-specific checks run later in the lowering
  // pipeline.
  pm.addPass(createKGENVerifierPass(
      KGENVerifierPassOptions{/*useMLIRVerifierOnly=*/true}));

  // Point materialized comptime values at their own storage before SROA and
  // Mem2Reg run: while their pointers still refer to interpreter storage, the
  // destination's address does not appear to escape and can be scalarized away.
  pm.addPass(createUpdateMaterialization());

  buildFirstOptPipeline(pm, options);
  buildLateOptPipeline(pm, options);
}

void KGEN::buildFirstOptPipeline(mlir::PassManager &pm,
                                 const CompilationOptions &options) {
  // Deduplicates functions coming out of the elaborator. Run before DCE as the
  // pass could introduce more dead symbols.
  pm.addPass(createEliminateDuplicateFunctions());

  // Run DCE first coming out of the elaborator.
  pm.addPass(createEliminateDeadSymbols());

  // Then immediately resolve compiler promises.
  pm.addPass(createResolveCompilerPromises());

  // We lower argument input conventions.
  pm.addNestedPass<FuncOp>(createLowerArgConventions());
  pm.addPass(createLowerCallingConventions());
  pm.addNestedPass<FuncOp>(createMem2Reg());

#ifndef MODULAR_PRODUCTION
  // Ensure no illegal parameters remain.
  pm.addNestedPass<FuncOp>(createEnsureNoParameters());
#endif

  pm.addPass(createInferFunctionAttrs());

  // Run the AutomaticInline pass with an inner function pass pipeline.
  auto buildAutomaticInlineFuncPasses = [options](mlir::OpPassManager &pm) {
    if (options.optimizationLevel < 1)
      return;
    pm.addPass(createSimplifyCF());
    pm.addPass(createSROA());
    pm.addPass(createMem2Reg());
    pm.addPass(createCanonicalizer());
    pm.addPass(mlir::createCSEPass());
    pm.addPass(createHoistTrivialInvariants());
    pm.addPass(createCanonicalizer());
    pm.addPass(createSimplifyCF());
    pm.addPass(createSROA());
    pm.addPass(createMem2Reg());
    pm.addPass(createStackReuse());
    pm.addPass(mlir::createCSEPass());
    pm.addPass(createCanonicalizer());
  };

  pm.addPass(createAutomaticInline(
      {options.debugLevel == CompilationOptions::DebugInfoLevel::kNoDebug
           ? InlinerDebugInfoUpdateTime::kNever
           : (options.optimizationLevel == 0
                  ? InlinerDebugInfoUpdateTime::kDeferred
                  : InlinerDebugInfoUpdateTime::kImmediate),
       options.optimizationLevel},
      std::move(buildAutomaticInlineFuncPasses)));

  // Process debuginfo based on the selected debugging level.
  if (options.debugLevel == CompilationOptions::DebugInfoLevel::kSynthetic)
    pm.addPass(createSynthesizeDebugInfo(
        {static_cast<llvm::dwarf::SourceLanguage>(options.debugInfoLanguage)}));

  // Guaranteed optimizations.
  pm.addNestedPass<FuncOp>(createSROA());
  pm.addNestedPass<FuncOp>(createMem2Reg());
  pm.addNestedPass<FuncOp>(createCanonicalizer());
  pm.addNestedPass<FuncOp>(createRaiseForLoops());
  pm.addNestedPass<FuncOp>(createLoopUnrolling({options.optimizationLevel}));

  if (options.optimizationLevel >= 1) {
    pm.addNestedPass<FuncOp>(createSROA());
    pm.addNestedPass<FuncOp>(createMem2Reg());
    pm.addNestedPass<FuncOp>(createSCCP());
    pm.addNestedPass<FuncOp>(createCanonicalizer());
    pm.addNestedPass<FuncOp>(mlir::createCSEPass());
  }

  pm.addPass(createEliminateDeadSymbols());
}

void KGEN::buildLateOptPipeline(mlir::PassManager &pm,
                                const CompilationOptions &options) {

  if (options.optimizationLevel >= 1) {
    pm.addPass(createArgPromotion());
    pm.addNestedPass<FuncOp>(createRaiseForLoops());
    pm.addNestedPass<FuncOp>(createLoopUnrolling({options.optimizationLevel}));
    pm.addNestedPass<FuncOp>(createSROA());
    pm.addNestedPass<FuncOp>(createMem2Reg());
    pm.addNestedPass<FuncOp>(createCanonicalizer());
    pm.addNestedPass<FuncOp>(mlir::createCSEPass());
  }

  if (options.optimizationLevel >= 2) {
    pm.addNestedPass<FuncOp>(createRaiseForLoops());
    pm.addNestedPass<FuncOp>(createLoopUnrolling({options.optimizationLevel}));
    pm.addNestedPass<FuncOp>(createSROA());
    pm.addNestedPass<FuncOp>(createMem2Reg());
    pm.addNestedPass<FuncOp>(createSimplifyCF());
    pm.addNestedPass<FuncOp>(createCanonicalizer());
    pm.addNestedPass<FuncOp>(mlir::createCSEPass());
  }

  pm.addNestedPass<FuncOp>(createLowerLoops());
  // Lower async functions and closures as late as possible.
  pm.addPass(createLowerClosures());
  pm.addPass(createLowerAsyncFunctions());
  if (options.optimizationLevel >= 2)
    pm.addPass(createDeadArgumentElimination());
  pm.addPass(createEliminateDeadSymbols());
}
