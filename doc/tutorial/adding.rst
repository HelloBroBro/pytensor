.. _adding:

====================
Baby Steps - Algebra
====================

Understanding Tensors
===========================

Before diving into PyTensor, it's essential to understand the fundamental
data structure it operates on: the *tensor*. A *tensor* is a multi-dimensional
array that serves as the foundation for symbolic computations.

tensors can represent anything from a single number (scalar) to
complex multi-dimensional arrays. Each tensor has a type that dictates its
dimensionality and the kind of data it holds.

For example, the following code creates a symbolic scalar and a symbolic matrix:

>>> x = pt.scalar('x')
>>> y = pt.matrix('y')

Here, `scalar` refers to a tensor with zero dimensions, while `matrix` refers
to a tensor with two dimensions. The same principles apply to tensors of other
dimensions.

For more information about tensors and their associated operations can be
found here: :ref:`tensor <libdoc_tensor>`.



Adding two Scalars
==================

To get us started with PyTensor and get a feel of what we're working with,
let's make a simple function: add two numbers together. Here is how you do
it:

>>> import numpy
>>> import pytensor.tensor as pt
>>> from pytensor import function
>>> x = pt.dscalar('x')
>>> y = pt.dscalar('y')
>>> z = x + y
>>> f = function([x, y], z)

And now that we've created our function we can use it:

>>> f(2, 3)
array(5.0)
>>> numpy.allclose(f(16.3, 12.1), 28.4)
True

Let's break this down into several steps. The first step is to define
two symbols (*Variables*) representing the quantities that you want
to add. Note that from now on, we will use the term
*Variable* to mean "symbol" (in other words,
*x*, *y*, *z* are all *Variable* objects). The output of the function
*f* is a ``numpy.ndarray`` with zero dimensions.

If you are following along and typing into an interpreter, you may have
noticed that there was a slight delay in executing the ``function``
instruction. Behind the scene, *f* was being compiled into C code.


.. note:

  A *Variable* is the main data structure you work with when
  using PyTensor. The symbolic inputs that you operate on are
  *Variables* and what you get from applying various operations to
  these inputs are also *Variables*. For example, when I type

  >>> x = pytensor.tensor.ivector()
  >>> y = -x

  *x* and *y* are both Variables, i.e. instances of the
  ``pytensor.graph.basic.Variable`` class. The
  type of both *x* and *y* is ``pytensor.tensor.ivector``.


**Step 1**

>>> x = pt.dscalar('x')
>>> y = pt.dscalar('y')

In PyTensor, all symbols must be typed. In particular, ``pt.dscalar``
is the type we assign to "0-dimensional arrays (`scalar`) of doubles
(`d`)". It is an PyTensor :ref:`type`.

``dscalar`` is not a class. Therefore, neither *x* nor *y*
are actually instances of ``dscalar``. They are instances of
:class:`TensorVariable`. *x* and *y*
are, however, assigned the pytensor Type ``dscalar`` in their ``type``
field, as you can see here:

>>> type(x)
<class 'pytensor.tensor.var.TensorVariable'>
>>> x.type
TensorType(float64, ())
>>> pt.dscalar
TensorType(float64, ())
>>> x.type is pt.dscalar
True

By calling ``pt.dscalar`` with a string argument, you create a
*Variable* representing a floating-point scalar quantity with the
given name. If you provide no argument, the symbol will be unnamed. Names
are not required, but they can help debugging.

More will be said in a moment regarding PyTensor's inner structure. You
could also learn more by looking into :ref:`graphstructures`.


**Step 2**

The second step is to combine *x* and *y* into their sum *z*:

>>> z = x + y

*z* is yet another *Variable* which represents the addition of
*x* and *y*. You can use the :ref:`pp <libdoc_printing>`
function to pretty-print out the computation associated to *z*.

>>> from pytensor import pp
>>> print(pp(z))
(x + y)


**Step 3**

The last step is to create a function taking *x* and *y* as inputs
and giving *z* as output:

>>> f = function([x, y], z)

The first argument to :func:`function <function.function>` is a list of Variables
that will be provided as inputs to the function. The second argument
is a single Variable *or* a list of Variables. For either case, the second
argument is what we want to see as output when we apply the function. *f* may
then be used like a normal Python function.

.. note::

    As a shortcut, you can skip step 3, and just use a variable's
    :func:`eval <pytensor.graph.basic.Variable.eval>` method.
    The :func:`eval` method is not as flexible
    as :func:`function` but it can do everything we've covered in
    the tutorial so far. It has the added benefit of not requiring
    you to import :func:`function` . Here is how :func:`eval` works:

    >>> import numpy
    >>> import pytensor.tensor as pt
    >>> x = pt.dscalar('x')
    >>> y = pt.dscalar('y')
    >>> z = x + y
    >>> numpy.allclose(z.eval({x : 16.3, y : 12.1}), 28.4)
    True

    We passed :func:`eval` a dictionary mapping symbolic pytensor
    variables to the values to substitute for them, and it returned
    the numerical value of the expression.

    :func:`eval` will be slow the first time you call it on a variable --
    it needs to call :func:`function` to compile the expression behind
    the scenes. Subsequent calls to :func:`eval` on that same variable
    will be fast, because the variable caches the compiled function.



Adding two Matrices
===================

You might already have guessed how to do this. Indeed, the only change
from the previous example is that you need to instantiate *x* and
*y* using the matrix Types:

>>> x = pt.dmatrix('x')
>>> y = pt.dmatrix('y')
>>> z = x + y
>>> f = function([x, y], z)

``dmatrix`` is the Type for matrices of doubles. Then we can use
our new function on 2D arrays:

>>> f([[1, 2], [3, 4]], [[10, 20], [30, 40]])
array([[ 11.,  22.],
       [ 33.,  44.]])

The variable is a NumPy array. We can also use NumPy arrays directly as
inputs:

>>> import numpy
>>> f(numpy.array([[1, 2], [3, 4]]), numpy.array([[10, 20], [30, 40]]))
array([[ 11.,  22.],
       [ 33.,  44.]])

It is possible to add scalars to matrices, vectors to matrices,
scalars to vectors, etc. The behavior of these operations is defined
by :ref:`broadcasting <libdoc_tensor_broadcastable>`.



Exercise
========

.. testcode::

   import pytensor
   a = pytensor.tensor.vector() # declare variable
   out = a + a ** 10               # build symbolic expression
   f = pytensor.function([a], out)   # compile function
   print(f([0, 1, 2]))

.. testoutput::

   [    0.     2.  1026.]


Modify and execute this code to compute this expression: a ** 2 + b ** 2 + 2 * a * b.


:download:`Solution<adding_solution_1.py>`
