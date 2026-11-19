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
//
// Modified by the ouromo project, November 2026: this file differs from the
// original in https://github.com/modular/modular (tag mojo/v1.2.0). The
// changes are in the history of https://github.com/ouromo/modular.
//

#include "AsyncRT/CompilerSupport/Context.h"
#include "AsyncRT/Runtime/ForkJoin.h"
#include "Mojo/KGENDialect/KGENOps.h"
#include "Mojo/ToolCommon/KGENPasses.h"
#include "Mojo/TransformUtils/CallGraphUtils.h"
#include "mlir/Analysis/SymbolTableAnalysis.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/ADT/SCCIterator.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/LineIterator.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/xxhash.h"
#include <cstdlib>
#include <mutex>
#include <set>

using namespace M;
using namespace KGEN;

namespace M::KGEN {
#define GEN_PASS_DEF_ELIMINATEDUPLICATEFUNCTIONS
#include "Mojo/KGENPasses.h.inc"
} // namespace M::KGEN

namespace {
struct DuplicateFuncOpEquivalenceInfo : public llvm::DenseMapInfo<FuncOp> {

  static unsigned getHashValue(const FuncOp cFunc) {
    if (!cFunc)
      return DenseMapInfo<FuncOp>::getHashValue(cFunc);

    llvm::hash_code hash = {};
    FuncOp func = const_cast<FuncOp &>(cFunc);
    StringAttr symNameAttrName = func.getSymNameAttrName();
    for (NamedAttribute namedAttr : cFunc->getAttrs()) {
      StringAttr attrName = namedAttr.getName();
      // Ignoring the symbol name.
      if (attrName == symNameAttrName)
        continue;
      hash = llvm::hash_combine(hash, namedAttr);
    }

    func.getBody()->walk([&](Operation *op) {
      hash = llvm::hash_combine(
          hash,
          mlir::OperationEquivalence::computeHash(
              op, /*hashOperands=*/mlir::OperationEquivalence::ignoreHashValue,
              /*hashResults=*/mlir::OperationEquivalence::ignoreHashValue,
              /*flags=*/mlir::OperationEquivalence::Flags::None));
    });

    return hash;
  }

  static bool isEqual(FuncOp lhs, FuncOp rhs) {
    if (lhs == rhs)
      return true;

    DictionaryAttr lhsDictAttr = lhs->getAttrDictionary();
    DictionaryAttr rhsDictAttr = rhs->getAttrDictionary();
    if (lhsDictAttr.size() != rhsDictAttr.size())
      return false;

    // Other than the symbol name, all other attributes need to be exact same.
    StringAttr symNameAttrName = lhs.getSymNameAttrName();
    for (auto [lhsAttr, rhsAttr] : llvm::zip_equal(lhsDictAttr, rhsDictAttr)) {
      if (lhsAttr.getName() == symNameAttrName && rhsAttr.getName())
        continue;
      if (lhsAttr != rhsAttr)
        return false;
    }

    // Compare function body, we can be more aggressive by using
    // `mlir::OperationEquivalence::Flags::IgnoreLocations`.
    // NOTE: Different constant operation orders will break the equivalence.
    return mlir::OperationEquivalence::isRegionEquivalentTo(
        &lhs.getBodyRegion(), &rhs.getBodyRegion(),
        /*flags=*/mlir::OperationEquivalence::Flags::None);
  }
};

struct CallGraphNode
    : public CallGraphNodeBase<CallGraphNode, FuncOp, KGENCallOpInterface> {
  using CallGraphNodeBase::CallGraphNodeBase;
  using CallGraphNodeBase::EdgeT;
  CallGraphNode(FuncOp func, AsyncRT::CPUDevice &cpuDevice)
      : CallGraphNodeBase(func),
        doneDedup(AsyncRT::AsyncValueRef<AsyncRT::Chain>::allocate(cpuDevice)) {
  }
  CallGraphNode(CallGraphNode &&other)
      : CallGraphNodeBase(std::move(other)),
        doneDedup(std::move(other.doneDedup)) {}

  /// Chain value to mark if the deduplication is done or not for synchronizing
  /// CallGraph dependencies.
  AsyncRT::AsyncValueRef<AsyncRT::Chain> doneDedup;

  /// The set of dependencies that has to be deduplicated before we can start
  /// working on this node.
  llvm::SmallPtrSet<CallGraphNode *, 6> dependencies;
};

struct CallGraph : public CallGraphBase<CallGraph, CallGraphNode> {
  // Construct and build the callgraph
  CallGraph(mlir::ModuleOp module, const SymbolTable &symtable,
            AsyncRT::CPUDevice &cpuDevice);

  bool shouldAddToGraph(CallOpT call, CallGraphNode *node) {
    // Skip external functions.
    return !node->func.isExternal();
  }

  void deduplicateNode(CallGraphNode *node);

  void startDeduplication();

  /// Once all equivalence classes are formed, redirect every callsite to the
  /// lexicographically smallest symbol in its callee's class
  void replaceCallSitesWithLexMin();

  void finishHandleNode(CallGraphNode *node) {
    node->doneDedup.copy().emplace();
    if (numWorkItems.fetch_sub(1) == 1)
      done.copy().emplace();
  }

  /// External node that has all the functions that do not have a caller in the
  /// Module as callees. This node is the entry node of the CallGraph for
  /// computing SCCs.
  CallGraphNode externalNode;

  /// Number of remaining work items.
  std::atomic<size_t> numWorkItems;
  /// All work items (i.e. functions might need to be deduplicated).
  SmallVector<CallGraphNode *> workItems;

  /// Reference to the AsyncRT cpuDevice for launch jobs in parallel.
  AsyncRT::CPUDevice &cpuDevice;

  /// Chain value to mark if all work items are done to mark main thread
  /// (this deduplication pass) to be done.
  AsyncRT::AsyncValueRef<AsyncRT::Chain> done;

  // Map from the target FuncOp to callsites that need to be redirected.
  DenseSet<FuncOp, DuplicateFuncOpEquivalenceInfo> uniqueFuncSet;

  // Maps each class representative's symbol name to the lexicographically
  // smallest symbol of that class.
  DenseMap<StringAttr, FuncOp> representativeToLexMin;

  // Guards `uniqueFuncSet` and `representativeToLexMin`.
  std::mutex setAccessMutex;

  // Number of deduplicated functions
  uint64_t numDeduped = 0;
};

struct EliminateDuplicateFunctionsPass
    : M::KGEN::impl::EliminateDuplicateFunctionsBase<
          EliminateDuplicateFunctionsPass> {
  void runOnOperation() override;
};
} // namespace

namespace llvm {
template <>
struct GraphTraits<CallGraph *> : public GraphTraits<CallGraph::BaseT *> {};
} // namespace llvm

CallGraph::CallGraph(mlir::ModuleOp module, const SymbolTable &symtable,
                     AsyncRT::CPUDevice &cpuDevice)
    : externalNode(nullptr, cpuDevice), numWorkItems(0), cpuDevice(cpuDevice),
      done(AsyncRT::AsyncValueRef<AsyncRT::Chain>::allocate(cpuDevice)),
      uniqueFuncSet() {
  CallGraphBase::build(module, symtable, cpuDevice);

  for (auto &[func, node] : nodes) {
    // We add an edge from externalNode to every other node. Note that it would
    // *NOT* change the topology of the callgraph nor does it introduce extra
    // cycles into the callgraph, as there is no incoming edge to the external
    // node.
    //
    // This is needed because EDF pass runs before DCE, thus there might
    // be unreachable cycles of dead symbols (which can not be easily identified
    // by `callers.empty()`). When that happens, the AsyncValue in those nodes
    // will remain to be uninitialized after the pass finishes. And AsyncRT,
    // unfortunately, does not know how to destruct uninitialized AsyncValue.
    externalNode.callsites.emplace_back(nullptr, &node);
  }

  // Capture the dependencies.
  for (auto scc = llvm::scc_begin(this); scc != llvm::scc_end(this); ++scc) {
    if (scc.hasCycle()) {
      // Cycles need extra handling. Mark them as ready (They will NOT be
      // deduplicated).
      for (CallGraphNode *node : *scc)
        node->doneDedup.copy().emplace();
      continue;
    }

    CallGraphNode *cn = scc->front();
    FuncOp toExam = cn->func;
    if (!toExam) // Skip entry node.
      continue;

    for (CallGraphNode *caller : cn->callers)
      caller->dependencies.insert(cn);

    workItems.push_back(cn);
  }
  // plus the entry node
  numWorkItems = workItems.size() + 1;
}

void CallGraph::deduplicateNode(CallGraphNode *node) {

  SmallVector<AnyAsyncValueRef> waitUtils;
  for (CallGraphNode *dep : node->dependencies)
    waitUtils.emplace_back(dep->doneDedup.copy());

  auto dedupFunc = [this, node](ArrayRef<AnyAsyncValueRef>) mutable {
    for (auto [call, callee] : node->callsites) {
      FuncOp replaceTo(nullptr);
      // Looks up potential replacement.
      {
        std::lock_guard<std::mutex> lock(setAccessMutex);
        auto it = uniqueFuncSet.find(callee->func);
        if (it != uniqueFuncSet.end() && *it != callee->func) {
          replaceTo = *it;
          ++numDeduped;
        }
      }
      if (replaceTo) {
        call.setCalleeAttr(SymbolConstantAttr::get(replaceTo));
      }
    }

    // Done rewriting the FuncOp, now updates the map.
    {
      std::lock_guard<std::mutex> lock(setAccessMutex);
      auto [representative, inserted] = uniqueFuncSet.insert(node->func);
      FuncOp &lexMin = representativeToLexMin[representative->getSymNameAttr()];
      if (inserted || node->func.getSymName() < lexMin.getSymName())
        lexMin = node->func;
    }

    // Mark the node as ready.
    finishHandleNode(node);
  };

  AsyncRT::andThenAsyncMoving(waitUtils, std::move(dedupFunc));
}

void CallGraph::startDeduplication() {
  for (CallGraphNode *node : workItems)
    deduplicateNode(node);

  finishHandleNode(&externalNode);
  AsyncRT::await(done);
}

void CallGraph::replaceCallSitesWithLexMin() {
  AsyncRT::ForkJoin state(cpuDevice);
  for (CallGraphNode *node : workItems) {
    state.fork([node, this] {
      for (auto [call, _] : node->callsites) {
        SymbolRefAttr calleeSymbol = call.getCalleeSymbol();
        if (!calleeSymbol)
          continue;
        auto it = representativeToLexMin.find(calleeSymbol.getLeafReference());
        // Callees that are external or part of a cycle are not deduplicated.
        if (it == representativeToLexMin.end())
          continue;
        call.setCalleeAttr(SymbolConstantAttr::get(it->second));
      }
    });
  }
  state.join();
}

namespace {
struct MergedFunctionNames {
  std::mutex mutex;
  std::set<std::string> lines;
};
MergedFunctionNames &mergedFunctionNames() {
  static MergedFunctionNames names;
  return names;
}

/// "FILE:LINE" of the declaration a function comes from, or "".
std::string declarationOf(FuncOp func) {
  std::string result;
  mlir::LocationAttr(func.getLoc()).walk([&](mlir::Location nested) {
    auto file = llvm::dyn_cast<mlir::FileLineColLoc>(nested);
    if (!file)
      return mlir::WalkResult::advance();
    result = (file.getFilename().getValue() + ":" + Twine(file.getLine())).str();
    return mlir::WalkResult::interrupt();
  });
  return result;
}

struct PreviousBuildFiles {
  std::string mergedPath, changedPath;
};
PreviousBuildFiles &previousBuildFiles() {
  static PreviousBuildFiles files;
  return files;
}

/// Which of several identical functions is kept depends on the functions the
/// program holds. A partial build (a small program that reaches the
/// functions an edit changed) reads the choices of the previous build of the
/// whole program, lines that begin with "FUNCTION\tKEPT", and adds a copy of a function
/// under the name the previous build kept for it when that function is not
/// in this module. A line that names a changed function is skipped: the
/// previous choice says nothing about new code.
void addPreviousBuildNames(mlir::ModuleOp module, mlir::SymbolTable &symtab) {
  const PreviousBuildFiles &files = previousBuildFiles();
  if (files.mergedPath.empty())
    return;
  auto buffer = llvm::MemoryBuffer::getFile(files.mergedPath);
  if (!buffer)
    return;
  llvm::StringSet<> changed;
  std::unique_ptr<llvm::MemoryBuffer> changedBuffer;
  if (!files.changedPath.empty()) {
    if (auto file = llvm::MemoryBuffer::getFile(files.changedPath)) {
      changedBuffer = std::move(*file);
      for (llvm::line_iterator line(*changedBuffer); !line.is_at_end(); ++line)
        changed.insert(*line);
    }
  }
  llvm::StringMap<FuncOp> byName;
  for (FuncOp func : module.getOps<FuncOp>())
    if (!func.isExternal())
      byName[func.getSymName()] = func;
  for (llvm::line_iterator line(**buffer); !line.is_at_end(); ++line) {
    auto [from, rest] = line->split('\t');
    StringRef to = rest.split('\t').first;
    if (changed.contains(from) || changed.contains(to))
      continue;
    auto it = byName.find(from);
    if (it == byName.end() || to.empty() || byName.count(to) ||
        mlir::SymbolTable::lookupSymbolIn(module, to))
      continue;
    // Add a copy under the kept name. It is identical to the function and
    // its name sorts first, so the pass redirects the calls to it.
    FuncOp func = it->second;
    FuncOp copy = cast<FuncOp>(func->clone());
    mlir::SymbolTable::setSymbolName(copy,
                                     StringAttr::get(module.getContext(), to));
    module.getBody()->getOperations().insertAfter(
        mlir::Block::iterator(func.getOperation()), copy.getOperation());
    // Later passes look the copy up in the symbol table of the analysis.
    symtab.insert(copy.getOperation());
    byName[to] = copy;
  }
}
struct FunctionCodeHashes {
  bool record = false;
  std::string previousPath;
  std::mutex mutex;
  std::vector<std::string> lines;
};
FunctionCodeHashes &functionCodeHashes() {
  static FunctionCodeHashes hashes;
  return hashes;
}

/// A hash of everything the pass compares in two functions: the text of the
/// function with its source locations, with its own name masked.
std::string codeHashOf(FuncOp func) {
  std::string text;
  llvm::raw_string_ostream os(text);
  mlir::OpPrintingFlags flags;
  flags.useLocalScope();
  flags.enableDebugInfo(/*enable=*/true, /*prettyForm=*/false);
  func->print(os, flags);
  StringRef name = func.getSymName();
  std::string masked;
  masked.reserve(text.size());
  for (size_t pos = 0;;) {
    size_t next = name.empty() ? std::string::npos : text.find(name.data(), pos, name.size());
    if (next == std::string::npos) {
      masked.append(text, pos);
      break;
    }
    masked.append(text, pos, next - pos);
    masked += '\x01';
    pos = next + name.size();
  }
  return llvm::utohexstr(llvm::xxh3_64bits(masked)) + "." +
         llvm::utohexstr(masked.size());
}

/// For a partial build, after the pass: a function of this module can have
/// the code of a function of the previous build that this module lacks, and
/// a build of the whole program would have merged the two. `hashes` holds
/// "NAME\tHASH" for the functions of this module. Returns true when a copy
/// of a function was added under the name the whole program keeps, so that
/// the pass has to run again.
bool mergeWithPreviousBuild(mlir::ModuleOp module, mlir::SymbolTable &symtab,
                            ArrayRef<std::pair<FuncOp, std::string>> hashes) {
  const std::string &path = functionCodeHashes().previousPath;
  if (path.empty())
    return false;
  auto buffer = llvm::MemoryBuffer::getFile(path);
  if (!buffer)
    return false;
  llvm::StringSet<> changed;
  if (!previousBuildFiles().changedPath.empty())
    if (auto file = llvm::MemoryBuffer::getFile(previousBuildFiles().changedPath))
      for (llvm::line_iterator line(**file); !line.is_at_end(); ++line)
        changed.insert(*line);
  // The first name, in sorting order, of the unchanged functions of the
  // previous build with each hash.
  llvm::StringMap<StringRef> firstName;
  for (llvm::line_iterator line(**buffer); !line.is_at_end(); ++line) {
    auto [name, hash] = line->split('\t');
    if (changed.contains(name))
      continue;
    auto [it, isNew] = firstName.try_emplace(hash, name);
    if (!isNew && name < it->second)
      it->second = name;
  }
  bool added = false;
  MergedFunctionNames &names = mergedFunctionNames();
  for (auto &[constFunc, hash] : hashes) {
    FuncOp func = constFunc;
    auto it = firstName.find(hash);
    if (it == firstName.end())
      continue;
    StringRef kept = it->second, name = func.getSymName();
    if (kept == name || mlir::SymbolTable::lookupSymbolIn(module, kept))
      continue;
    if (name < kept) {
      // The whole program keeps this function and points the callers of
      // the other one to it. The line tells the driver of the build.
      std::lock_guard<std::mutex> lock(names.mutex);
      names.lines.insert(
          (kept + "\t" + name + "\t\t" + declarationOf(func)).str());
      continue;
    }
    FuncOp copy = cast<FuncOp>(func->clone());
    mlir::SymbolTable::setSymbolName(
        copy, StringAttr::get(module.getContext(), kept));
    module.getBody()->getOperations().insertAfter(
        mlir::Block::iterator(func.getOperation()), copy.getOperation());
    symtab.insert(copy.getOperation());
    added = true;
  }
  return added;
}
} // namespace

void M::KGEN::recordFunctionCodeHashes(std::string previousPath) {
  FunctionCodeHashes &hashes = functionCodeHashes();
  hashes.record = true;
  hashes.previousPath = std::move(previousPath);
}

std::vector<std::string> M::KGEN::takeFunctionCodeHashes() {
  FunctionCodeHashes &hashes = functionCodeHashes();
  std::lock_guard<std::mutex> lock(hashes.mutex);
  std::vector<std::string> result = std::move(hashes.lines);
  hashes.lines.clear();
  llvm::sort(result);
  return result;
}

void M::KGEN::usePreviousBuildMergedFunctions(std::string mergedPath,
                                              std::string changedPath) {
  previousBuildFiles() = {std::move(mergedPath), std::move(changedPath)};
}

std::vector<std::string> M::KGEN::takeMergedFunctionNames() {
  MergedFunctionNames &names = mergedFunctionNames();
  std::lock_guard<std::mutex> lock(names.mutex);
  std::vector<std::string> result(names.lines.begin(), names.lines.end());
  names.lines.clear();
  return result;
}

void EliminateDuplicateFunctionsPass::runOnOperation() {
  VerboseCompilerTimeTraceScope traceScope("eliminateDuplicateFunctions");

  mlir::ModuleOp module = getOperation();
  const SymbolTable &symtab =
      getAnalysis<mlir::SymbolTableAnalysis>().getTopLevelSymbolTable();
  addPreviousBuildNames(module, const_cast<mlir::SymbolTable &>(symtab));
  auto &cpuDevice = *loadContext(&getContext())->get<AsyncRT::CPUDevice>();
  mlir::SymbolTable &mutableSymtab = const_cast<mlir::SymbolTable &>(symtab);
  // A partial build can add a function and run again; see
  // mergeWithPreviousBuild. Each round merges at least one more function.
  for (int round = 0; round != 16; ++round) {
    CallGraph cg(module, symtab, cpuDevice);
    cg.startDeduplication();
    // Record each function that calls are redirected away from, with the
    // function kept in its place, for the manifest of the split cache. This
    // comes before the redirection, which changes function bodies and with
    // them the hashes the set is keyed by.
    {
      MergedFunctionNames &names = mergedFunctionNames();
      std::lock_guard<std::mutex> lock(names.mutex);
      for (CallGraphNode *node : cg.workItems) {
        auto rep = cg.uniqueFuncSet.find(node->func);
        if (rep == cg.uniqueFuncSet.end())
          continue;
        auto kept = cg.representativeToLexMin.find(rep->getSymNameAttr());
        if (kept == cg.representativeToLexMin.end() || kept->second == node->func)
          continue;
        names.lines.insert((node->func.getSymName() + "\t" +
                            kept->second.getSymName() + "\t" +
                            declarationOf(node->func) + "\t" +
                            declarationOf(kept->second))
                               .str());
      }
    }
    cg.replaceCallSitesWithLexMin();
    this->numDeduped += cg.numDeduped;
    FunctionCodeHashes &hashes = functionCodeHashes();
    if (!hashes.record)
      break;
    // The code of each function as the rest of the compiler sees it, for
    // the manifest of the split cache and for the comparison with the
    // previous build.
    std::vector<std::pair<FuncOp, std::string>> hashOf(cg.workItems.size());
    {
      AsyncRT::ForkJoin state(cpuDevice);
      for (size_t i = 0, e = cg.workItems.size(); i != e; ++i)
        state.fork([i, &cg, &hashOf] {
          FuncOp func = cg.workItems[i]->func;
          hashOf[i] = {func, codeHashOf(func)};
        });
      state.join();
    }
    if (mergeWithPreviousBuild(module, mutableSymtab, hashOf))
      continue;
    std::lock_guard<std::mutex> lock(hashes.mutex);
    for (auto &[func, hash] : hashOf)
      hashes.lines.push_back((func.getSymName() + "\t" + hash).str());
    break;
  }
  // NOTE: We do not erase the duplicated function in the pass but rely later
  // passes to cleanup. A duplicate function is not always a dead symbol as it
  // might be referenced by operations other than KGENCallOpInterface.
}
