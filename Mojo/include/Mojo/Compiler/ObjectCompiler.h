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

#ifndef KGEN_COMPILER_OBJECTCOMPILER_H
#define KGEN_COMPILER_OBJECTCOMPILER_H

#include "Cache/BlobCache.h"
#include "Cache/CachedTransform.h"
#include "Mojo/Compiler/LLVMIRUtils.h"
#include "Mojo/ExecutionEngine/ExecutionEngine.h"
#include "Mojo/KGENDialect/KGENUtils.h"
#include "Mojo/ToolCommon/CompilationOptions.h"
#include "Mojo/ToolCommon/PassManagerConfigOptions.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/PassManager.h"
#include "llvm/ADT/SmallSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/IR/ModuleSummaryIndex.h"
#include <filesystem>
#include <mutex>
#include <string>
#include <vector>

namespace llvm {
class LLVMContext;
class Module;
class TargetMachine;
class DataLayout;
namespace orc {
class ExecutionSession;
} // namespace orc
} // namespace llvm

namespace M::KGEN {
struct SymbolAndMCInfo;
class TargetBackend;

//===----------------------------------------------------------------------===//
// ObjectCompiler
//===----------------------------------------------------------------------===//

/// The purpose of this class is to provide methods to lower concrete KGEN
/// functions to LLVM, and then to objects.
class ObjectCompiler {
public:
  /// Construct an ObjectCompiler that infers the exports from the module.
  static ErrorOr<std::unique_ptr<ObjectCompiler>>
  create(StringRef basePath, CompilationOptions options, bool isJIT,
         MLIRContext &context,
         PassManagerConfigOptions pmOptions = PassManagerConfigOptions());

  /// Emit the module to a object archive. If outKeyHash is provided, it will
  /// be populated with the hash of the key used to cache the module.
  ErrorOr<BufferRef> emitArchive(OwningOpRef<ModuleOp> module,
                                 bool emitAssembly = false,
                                 std::string *outKeyHash = nullptr);

  /// Lower the given module to LLVM. Returns the LLVM module on success, and
  /// nullptr on failure.
  ErrorOr<std::unique_ptr<llvm::Module>>
  lowerAllFuncsToLLVM(llvm::LLVMContext &ctx, ModuleOp module);

  /// Lower the given module to LLVM and run LLVM optimizations.
  ErrorOrSuccess
  lowerAllFuncsToLLVMAndOptimize(ModuleOp module,
                                 LLVMModuleAndContext &llvmModule);

  /// Slices the call graph for all exported symbols to produce a standalone
  /// LLVMIR file. The LLVMIR output is written to the provided stream.
  ErrorOrSuccess emitLLVMIR(ModuleOp module, llvm::raw_pwrite_stream &os);

  /// Slices the call graph for all exported symbols to produce a standalone
  /// assembly file. The assembly output is written to the provided stream.
  ErrorOrSuccess emitAssembly(OwningOpRef<ModuleOp> module,
                              llvm::raw_pwrite_stream &os);

  /// Write bitcode representation of the llvmModule using correct
  /// BitcodeWriter, e.g. it uses custom BitcodeWriter17  for Metal
  /// target.
  static ErrorOrSuccess emitBitcode(llvm::Module &llvmModule,
                                    llvm::raw_pwrite_stream &os);

  /// Slices the call graph for all exported symbols to produce a standalone
  /// shared object file. The output is written to the provided stream.
  ErrorOrSuccess emitSharedObject(OwningOpRef<ModuleOp> module,
                                  llvm::raw_pwrite_stream &os);

  /// Writes C++ function declarations for all exported symbols.
  LogicalResult emitCXXHeader(ModuleOp module, StringRef filename,
                              raw_ostream &os);

  ErrorOr<DenseMap<uint64_t, DenseMap<EmitAs, BufferRef>>> emitOffloadKernels(
      OwningOpRef<ModuleOp> module,
      llvm::DenseMap<uint64_t, llvm::SmallSet<EmitAs, 4>> kernelEmissionKinds);

  /// Get a reference to the object compiler's transform cache.
  RCRef<Cache::TransformCache> getTransformCache() {
    return transformCache.copy();
  }

  /// Get whether compilation is for JIT.
  bool getIsJIT() const { return isJIT; }

  /// Get the bitcode libraries (mutable for usage tracking and modification).
  SmallVector<std::pair<bool, Attribute>> &getBitcodeLibs() {
    return bitcodeLibs;
  }

private:
  /// Construct an ObjectCompiler with a specific set of exports. `backend` is
  /// the (non-null) target backend resolved by `create` from `options`.
  ObjectCompiler(
      RCRef<Cache::BlobCacheBackend> transformCache, CompilationOptions options,
      const TargetBackend &backend, bool isJIT, MLIRContext &context,
      const std::string &linker,
      PassManagerConfigOptions pmOptions = PassManagerConfigOptions());

  /// Lower the given LLVM module to an object file (parLLC = false) or
  /// multiple object files per function (parLLC = true).
  AsyncRT::AsyncValueRef<SymbolAndMCInfo> lowerLLVMModuleToObjects(
      llvm::unique_function<LLVMModuleAndContext()> produceModule, Location loc,
      llvm::TargetMachine &targetMachine, bool parLLC,
      std::optional<size_t> moduleIdx, unsigned numFunctionsBase);

  /// Split llvm module and compile them in parallel towards the end of codegen
  /// but stop before AsmPrint. Return the MC compilation results.
  SmallVector<AsyncRT::AnyAsyncValueRef> emitArchiveParallelCompilation(
      LLVMModuleAndContext llvmModule, Location opLoc,
      llvm::TargetMachine &targetMachine,
      llvm::StringMap<llvm::GlobalValue::LinkageTypes> &symbolLinkageTypes);

  /// Link parallel compilation results and call AsmPrint to generate one object
  /// file.
  ErrorOr<WriteableBufferRef> emitArchiveMCLinking(
      MutableArrayRef<AnyAsyncValueRef> values, StringRef moduleName,
      bool emitAssembly,
      llvm::StringMap<llvm::GlobalValue::LinkageTypes> &symbolLinkageTypes,
      const llvm::StringMap<unsigned> &originalFnOrdering);

  /// Generate saveTempsPrefix files.
  ErrorOrSuccess emitArchiveSaveTemps(ModuleOp module, StringRef moduleName);

  /// Compile one chunk of a module split per function through the split
  /// cache. `splitNames` holds, for each split of the chunk, the names of the
  /// values it defines. Each split is carved out of the chunk module and
  /// hashed; a split whose object file is not in the cache is optimized and
  /// compiled. The paths of the object files go to `splitCacheObjects`.
  AsyncRT::AsyncValueRef<SymbolAndMCInfo> compileChunkThroughSplitCache(
      llvm::unique_function<LLVMModuleAndContext()> produceChunk,
      std::vector<std::vector<std::string>> splitNames, Location loc,
      uint64_t chunkIdx);

  /// Combine the object files in `splitCacheObjects` into one relocatable
  /// object file, and then remove the oldest objects of the cache if it is
  /// over its size limit.
  ErrorOr<WriteableBufferRef> linkSplitCacheObjects();

  /// The caches needed for compilation.
  RCRef<Cache::TransformCache> transformCache;

  /// The compilation options to use.
  CompilationOptions options;

  /// The target backend for `options`' target. Always non-null: `create`
  /// resolves it from `options.targetTriple` and errors out when the target
  /// has no registered backend (i.e. an unsupported target).
  const TargetBackend &backend;

  /// This is a bit odd, but since we use this layer to generate code for cases
  /// where we aren't going to immediately execute it, we need to be able to
  /// change the codegen mode.
  bool isJIT;

  /// PassManager configuration options.
  PassManagerConfigOptions pmOptions;

  /// The MLIR context.
  MLIRContext &context;

  /// The AsyncRT cpuDevice.
  AsyncRT::CPUDevice &cpuDevice;

  /// Mutex to protect deduplicating shared
  /// data structure among parallel splits.
  std::mutex dedupMutex;

  /// StringSet to deduplicate functions among parallel splits.
  llvm::StringSet<> seenCodeGenFns;

  /// Mutex to protect deduplicating TargetMachine to save peak memory
  /// footprint.
  std::mutex tmMutex;

  /// True while `emitArchive` builds through the split cache.
  bool useSplitCache = false;

  /// Mutex to protect `splitCacheObjects`.
  std::mutex splitCacheMutex;

  /// The object file of each split of the current build, with the position
  /// of the split, which gives the link order.
  std::vector<std::pair<uint64_t, std::string>> splitCacheObjects;

  /// The manifest line of each split of the current build, with the position
  /// of the split. Filled when the options ask for a manifest.
  std::vector<std::pair<uint64_t, std::string>> splitCacheManifestLines;

  /// Name of the system linker.
  std::string linker;

  /// Current bitcode libraries with usage tracking.
  /// Each pair contains: (used_flag, library_attribute).
  SmallVector<std::pair<bool, Attribute>> bitcodeLibs;
};

/// Setup the machine properties from the provided target.
ErrorOr<std::unique_ptr<llvm::TargetMachine>>
createTargetMachine(const CompilationOptions &options, bool isJIT);

namespace LLVM {
/// Write the specified module to the specified raw output stream using
/// bitcode format version 5.0.
///
/// For streams where it matters, the given stream should be in "binary"
/// mode.
///
/// If \c ShouldPreserveUseListOrder, encode the use-list order for each \a
/// Value in \c M.  These will be reconstructed exactly when \a M is
/// deserialized.
///
/// If \c Index is supplied, the bitcode will contain the summary index
/// (currently for use in ThinLTO optimization).
///
/// \p GenerateHash enables hashing the Module and including the hash in the
/// bitcode (currently for use in ThinLTO incremental build).
///
/// If \p ModHash is non-null, when GenerateHash is true, the resulting
/// hash is written into ModHash. When GenerateHash is false, that value
/// is used as the hash instead of computing from the generated bitcode.
/// Can be used to produce the same module hash for a minimized bitcode
/// used just for the thin link as in the regular full bitcode that will
/// be used in the backend.
void WriteBitcode17ToFile(const llvm::Module &M, llvm::raw_ostream &Out,
                          bool ShouldPreserveUseListOrder = false,
                          const llvm::ModuleSummaryIndex *Index = nullptr,
                          bool GenerateHash = false,
                          llvm::ModuleHash *ModHash = nullptr);

void WriteBitcode19ToFile(const llvm::Module &M, llvm::raw_ostream &Out,
                          bool ShouldPreserveUseListOrder = false,
                          const llvm::ModuleSummaryIndex *Index = nullptr,
                          bool GenerateHash = false,
                          llvm::ModuleHash *ModHash = nullptr);

void WriteBitcode21ToFile(const llvm::Module &M, llvm::raw_ostream &Out,
                          bool ShouldPreserveUseListOrder = false,
                          const llvm::ModuleSummaryIndex *Index = nullptr,
                          bool GenerateHash = false,
                          llvm::ModuleHash *ModHash = nullptr);

} // namespace LLVM

} // namespace M::KGEN

#endif // KGEN_COMPILER_OBJECTCOMPILER_H
