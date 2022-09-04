# MIT License
#
# Copyright (C) The Adversarial Robustness Toolbox (ART) Authors 2022
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This module implements certified adversarial training following techniques from works such as:
    | Paper link: http://proceedings.mlr.press/v80/mirman18b/mirman18b.pdf
    | Paper link: https://arxiv.org/pdf/1810.12715.pdf
"""
import logging
import math
import random
import sys
import numpy as np

from tqdm import tqdm
from art.defences.trainer.trainer import Trainer
from art.attacks.evasion.projected_gradient_descent.projected_gradient_descent import ProjectedGradientDescent
from art.utils import check_and_transform_label_format

if sys.version_info >= (3, 8):
    from typing import TypedDict, Optional, Any, Tuple, Union, TYPE_CHECKING
else:
    from typing import Optional, Any, Tuple, Union, TYPE_CHECKING
    from functools import reduce

if TYPE_CHECKING:
    import torch
    from art.utils import CERTIFIER_TYPE

    # if we use python 3.8 or higher use the more informative TypedDict for type hinting
    if sys.version_info >= (3, 8):

        class PGDParamDict(TypedDict):
            """
            A TypedDict class to define the types in the pgd_params dictionary.
            """

            eps: float
            eps_step: float
            max_iter: int
            num_random_init: int
            batch_size: int

    else:
        PGDParamDict: dict[str, Union[int, float]]


logger = logging.getLogger(__name__)


class DefaultLinearScheduler:
    """
    Class implementing a simple linear scheduler to grow the certification radius.
    """

    def __init__(self, step_per_epoch: float, initial_bound: float = 0.0) -> None:
        """
        Create a .DefaultLinearScheduler instance.

        :param step_per_epoch: how much to increase the certification radius every epoch
        :param initial_bound: the initial bound to increase from
        """
        self.step_per_epoch = step_per_epoch
        self.bound = initial_bound

    def step(self) -> float:
        """
        Grow the certification radius by self.step_per_epoch
        """
        self.bound += self.step_per_epoch
        return self.bound


class AdversarialTrainerCertified(Trainer):
    """
    Class performing certified adversarial training from methods such as
    | Paper link: http://proceedings.mlr.press/v80/mirman18b/mirman18b.pdf
    | Paper link: https://arxiv.org/pdf/1810.12715.pdf

    """

    def __init__(
        self,
        classifier: "CERTIFIER_TYPE",
        nb_epochs: Optional[int] = 20,
        bound: float = 0.1,
        loss_weighting: float = 0.1,
        batch_size: int = 10,
        concrete_to_zonotope: Optional[Any] = None,
        use_certification_schedule: bool = True,
        certification_schedule: Optional[Any] = None,
        pgd_params: Optional["PGDParamDict"] = None,
    ) -> None:
        """
        Create an :class:`.AdversarialTrainerCertified` instance.

        Default values are for MNIST in pixel range 0-1.

        :param classifier: Classifier to train adversarially.
        :param pgd_params: A dictionary containing the specific parameters relating to regular PGD training.
                           Must contain  the following keys:

                               "eps": Maximum perturbation that the attacker can introduce.
                               "eps_step": Attack step size (input variation) at each iteration.
                               "max_iter": The maximum number of iterations.
                               "batch_size": Size of the batch on which adversarial samples are generated.
                               "num_random_init": Number of random initialisations within the epsilon ball.
                                                  For num_random_init=0 starting at the original input.

                           If not provided, we will default to typical MNIST values.
        :param bound: The perturbation range for the zonotope. Will be ignored if a certification_schedule is used.
        :param loss_weighting: Weighting factor for the certified loss.
        :param concrete_to_zonotope:  Optional argument. Function which takes in a concrete data point and the bound
                                      and converts the datapoint to the zonotope domain via:

                                                processed_sample = concrete_to_zonotope(sample, bound)

                                      If left as None, by default we apply the bound to every feature equally and
                                      adjust the zonotope such that it remains in the 0 - 1 range.

        :param nb_epochs: Number of training epochs.
        :param use_certification_schedule: If to use a training schedule for the certification radius.
        :param certification_schedule: Schedule for gradually increasing the certification radius. Empirical studies
                                       have shown that this is often required to achieve best performance.
                                       Either True to use the default linear scheduler,
                                       or a class with a .step() method that returns the updated bound every epoch.

        :param batch_size: Size of batches to use for certified training. NB, this will run the data
                           sequentially accumulating gradients over the batch size.
        """
        super().__init__(classifier=classifier)  # type: ignore
        self._classifier: "CERTIFIER_TYPE"
        self.pgd_params: "PGDParamDict"

        if pgd_params is None:
            self.pgd_params = {"eps": 0.3, "eps_step": 0.05, "max_iter": 20, "batch_size": 128, "num_random_init": 1}
        else:
            self.pgd_params = pgd_params

        self.nb_epochs = nb_epochs
        self.loss_weighting = loss_weighting
        self.bound = bound
        self.use_certification_schedule = use_certification_schedule
        self.certification_schedule = certification_schedule
        self.batch_size = batch_size
        self.concrete_to_zonotope = concrete_to_zonotope
        # Setting up adversary and perform adversarial training:
        self.attack = ProjectedGradientDescent(
            estimator=self._classifier,
            eps=self.pgd_params["eps"],
            eps_step=self.pgd_params["eps_step"],
            max_iter=self.pgd_params["max_iter"],
            num_random_init=self.pgd_params["num_random_init"],
        )

    def fit(  # pylint: disable=W0221
        self,
        x: np.ndarray,
        y: np.ndarray,
        certification_loss: Any = "interval_loss_cce",
        batch_size: Optional[int] = None,
        nb_epochs: Optional[int] = None,
        training_mode: bool = True,
        scheduler: Optional[Any] = None,
        **kwargs,
    ) -> None:
        """
        Fit the classifier on the training set `(x, y)`.

        :param x: Training data.
        :param y: Target values (class labels) one-hot-encoded of shape (nb_samples, nb_classes) or index labels of
                  shape (nb_samples,).
        :param certification_loss: Which certification loss function to use. Either "interval_loss_cce"
                                   or "max_logit_loss". By default will use interval_loss_cce.
                                   Alternatively, a user can supply their own loss function which takes in as input
                                   the zonotope predictions of the form () and labels of the from () and returns a
                                   scalar loss.
        :param batch_size: Size of batches to use for certified training. NB, this will run the data
                           sequentially accumulating gradients over the batch size.
        :param nb_epochs: Number of epochs to use for training.
        :param training_mode: `True` for model set to training mode and `'False` for model set to evaluation mode.
        :param scheduler: Learning rate scheduler to run at the start of every epoch.
        :param kwargs: Dictionary of framework-specific arguments. This parameter is not currently supported for PyTorch
               and providing it takes no effect.
        """
        import torch  # lgtm [py/repeated-import]

        if batch_size is None:
            batch_size = self.batch_size

        if nb_epochs is not None:
            epochs: int = nb_epochs
        elif self.batch_size is not None:
            epochs = self.batch_size

        # Set model mode
        self._classifier._model.train(mode=training_mode)  # pylint: disable=W0212

        if self._classifier._optimizer is None:  # pragma: no cover # pylint: disable=W0212
            raise ValueError("An optimizer is needed to train the model, but none is provided.")

        y = check_and_transform_label_format(y, nb_classes=self._classifier.nb_classes)

        # Apply preprocessing
        x_preprocessed, y_preprocessed = self._classifier.apply_preprocessing(x, y, fit=True)

        # Check label shape
        y_preprocessed = self._classifier.reduce_labels(y_preprocessed)

        num_batch = int(np.ceil(len(x_preprocessed) / float(self.pgd_params["batch_size"])))
        ind = np.arange(len(x_preprocessed))
        from sklearn.utils import shuffle

        x_cert = np.copy(x_preprocessed)
        y_cert = np.copy(y_preprocessed)

        # Start training
        if self.use_certification_schedule:
            if self.certification_schedule is None:
                certification_schedule_function = DefaultLinearScheduler(
                    step_per_epoch=self.bound / epochs, initial_bound=0.0
                )
        else:
            bound = self.bound

        for _ in tqdm(range(epochs)):
            if self.use_certification_schedule:
                bound = certification_schedule_function.step()
            # Shuffle the examples
            random.shuffle(ind)

            # Train for one epoch
            pbar = tqdm(range(num_batch))
            for m in pbar:
                certified_loss = torch.tensor(0.0).to(self._classifier.device)
                samples_certified = 0
                # Zero the parameter gradients
                self._classifier._optimizer.zero_grad()  # pylint: disable=W0212

                # get the certified loss
                x_cert, y_cert = shuffle(x_cert, y_cert)
                for i, (sample, label) in enumerate(zip(x_cert, y_cert)):

                    self.set_forward_mode("concrete")
                    concrete_pred = self._classifier.model.forward(np.expand_dims(sample, axis=0))
                    concrete_pred = torch.argmax(concrete_pred)

                    if self.concrete_to_zonotope is None:
                        if sys.version_info >= (3, 8):
                            eps_bound = np.eye(math.prod(self._classifier.input_shape)) * bound
                        else:
                            eps_bound = np.eye(reduce(lambda x, y: x * y, self._classifier.input_shape)) * bound

                        processed_sample, eps_bound = self._classifier.pre_process(cent=np.copy(sample), eps=eps_bound)
                        processed_sample = np.expand_dims(processed_sample, axis=0)
                    else:
                        processed_sample = self.concrete_to_zonotope(sample, bound)

                    # Perform prediction
                    self.set_forward_mode("abstract")
                    bias, eps = self._classifier.model.forward(eps=eps_bound, cent=processed_sample)
                    bias = torch.unsqueeze(bias, dim=0)

                    if certification_loss == "max_logit_loss":
                        certified_loss += self._classifier.max_logit_loss(
                            prediction=torch.cat((bias, eps)), target=np.expand_dims(label, axis=0)
                        )
                    elif certification_loss == "interval_loss_cce":
                        certified_loss += self._classifier.interval_loss_cce(
                            prediction=torch.cat((bias, eps)),
                            target=torch.from_numpy(np.expand_dims(label, axis=0)).to(self._classifier.device),
                        )
                    else:
                        certified_loss += certification_loss(torch.cat((bias, eps)), np.expand_dims(label, axis=0))

                    certification_results = []
                    bias = torch.squeeze(bias).detach().cpu().numpy()
                    eps = eps.detach().cpu().numpy()

                    for k in range(self._classifier.nb_classes):
                        if k != concrete_pred:
                            cert_via_sub = self._classifier.certify_via_subtraction(
                                predicted_class=concrete_pred, class_to_consider=k, cent=bias, eps=eps
                            )
                            certification_results.append(cert_via_sub)

                    if all(certification_results):
                        samples_certified += 1

                    if (i + 1) % batch_size == 0 and i > 0:
                        break

                certified_loss /= batch_size
                # Concrete PGD loss
                i_batch = np.copy(
                    x_preprocessed[ind[m * self.pgd_params["batch_size"] : (m + 1) * self.pgd_params["batch_size"]]]
                ).astype("float32")
                o_batch = y_preprocessed[
                    ind[m * self.pgd_params["batch_size"] : (m + 1) * self.pgd_params["batch_size"]]
                ]

                # Perform prediction
                self.set_forward_mode("concrete")
                self.attack = ProjectedGradientDescent(
                    estimator=self._classifier,
                    eps=self.pgd_params["eps"],
                    eps_step=self.pgd_params["eps_step"],
                    max_iter=self.pgd_params["max_iter"],
                    num_random_init=self.pgd_params["num_random_init"],
                )
                i_batch = self.attack.generate(i_batch, y=o_batch)
                self._classifier.model.zero_grad()
                model_outputs = self._classifier.model.forward(i_batch)
                acc = self._classifier.get_accuracy(model_outputs, o_batch)

                # Form the loss function
                pgd_loss = self._classifier.concrete_loss(
                    model_outputs, torch.from_numpy(o_batch).to(self._classifier.device)
                )  # pylint: disable=W0212

                pbar.set_description(
                    f"Bound {bound:.2f}: Loss {pgd_loss:.2f} Cert Loss {certified_loss:.2f} "
                    f"Acc {acc:.2f} Cert Acc {samples_certified / batch_size:.2f}"
                )

                loss = certified_loss * self.loss_weighting + pgd_loss * (1 - self.loss_weighting)
                # Do training
                if self._classifier._use_amp:  # pragma: no cover # pylint: disable=W0212
                    from apex import amp  # pylint: disable=E0611

                    with amp.scale_loss(loss, self._classifier._optimizer) as scaled_loss:  # pylint: disable=W0212
                        scaled_loss.backward()

                else:
                    loss.backward()

                self._classifier._optimizer.step()  # pylint: disable=W0212

            if scheduler is not None:
                scheduler.step()

    def predict(self, x: np.ndarray, **kwargs) -> Union["torch.Tensor", Tuple["torch.Tensor", "torch.Tensor"]]:
        """
        Perform prediction using the adversarially trained classifier.

        :param x: Input samples.
        :param kwargs: Other parameters to be passed on to the `predict` function of the classifier.
        :return: Predictions for test set.
        """
        return self._classifier.model.forward(x, **kwargs)

    def set_forward_mode(self, mode: str) -> None:
        """
        Helper function to set the forward mode of the model

        :param mode: either concrete or abstract signifying how to run the forward pass
        """
        self._classifier._model._model.set_forward_mode(mode)  # pylint: disable=W0212
